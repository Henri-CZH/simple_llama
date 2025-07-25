import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Optional, Tuple
from ..kernels import *
from .model_config import LlamaConfig
from .RotaryEmbedding import LlamaRotaryEmbedding

class FusedAttention(nn.Module):
    def __init__(self,  config: LlamaConfig, cache_k=None, cache_v=None):
        super().__init__()
        self.config= config

        # K V 头数相同，但和 Q 可能不同
        self.num_kv_heads = config.num_heads if config.num_kv_heads is None else config.num_kv_heads
        self.head_dim = config.head_dim if config.head_dim is not None else config.hidden_size // config.num_heads
        
        self.num_q_heads = config.num_heads
        self.hidden_size = config.num_heads * self.head_dim

        self.q_proj = nn.Linear(self.hidden_size, self.hidden_size, bias=False, dtype=torch.float16) # (in, out)
        self.kv_proj_weight = nn.Parameter(torch.rand(self.num_kv_heads * self.head_dim * 2, self.hidden_size, dtype=torch.float16)) # (out, in)
        self.o_proj = nn.Linear(self.hidden_size, self.hidden_size, bias=False, dtype=torch.float16) # (in, out)

    def context_forward(
        self,
        x: torch.Tensor, # (B, S, D)
        atten_info,
        layer_index:int,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None, # (B, S, Hd)
        qk_scale = None,
    ):         
        batch_size, seq_len, _ = x.shape  # prefill: (B, S, D); decode: (B, 1, D)
        x = x.view(-1, self.hidden_size) # (B, S, D)->(B*S, D)
        
        # 1. 计算 Q K V 并且 reshape 它们尺寸, 方便后续做 self-attention
        xq = self.q_proj(x) # (B*S, D)@(D, D)->(B*S, D)
        k_proj_weight, v_proj_weight = torch.split(self.kv_proj_weight, self.num_kv_heads * self.head_dim, dim=0) # (KVH*Hd*2, D)->2个(KVH*Hd, D)
        xk = F.linear(x, k_proj_weight) # (B*S, D)@(D, KVH*hd)->(B*S, KVH*Hd)
        xv = F.linear(x, v_proj_weight) # (B*S, D)@(D, KVH*hd)->(B*S, KVH*Hd)

        # 2. 应用旋转位置编码到 Q 和 K, 将 xk, xv 合并, 并写入缓存
        xq = xq.view(-1, self.num_q_heads, self.head_dim) # (B*S, D)->(B*S, H, Hd)
        xk = xk.view(-1, self.num_kv_heads, self.head_dim) # (B*S, D)->(B*S, KVH, Hd)
        xv = xv.view(-1, self.num_kv_heads, self.head_dim) # (B*S, D)->(B*S, KVH, Hd)

        cos, sin = position_embeddings
        xq, xk = rope_emb_forward(xq, xk, cos, sin, batch_size, seq_len)
        combined_kv = torch.cat([xk, xv], dim=-2) # (B*S, 2*num_kv_heads, head_dim)  
        update_kv_buffer(combined_kv, atten_info.cur_select_index, atten_info.kv_buffer[layer_index])

        # 3. sel-attention. flashattention 计算: softmax(qk^t) * v
        output = flash_attentionv2_no_pad(
            xq, xk, xv,
            qk_scale,
            atten_info.b_start_loc, 
            atten_info.b_seq_len, 
            seq_len,
        )

        # output = output.view(batch_size*seq_len, self.hidden_size)
        output = output.view(batch_size, seq_len, self.hidden_size)
        # 4. attention 输出做线性变换
        output = self.o_proj(output) # (B, S, D)@(D, D)->(B, S, D)
        return output

    def token_forward(self, 
        x: torch.Tensor,
        atten_info,
        layer_index:int,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        qk_scale = None, 
    ):
        batch_size, seq_len, _ = x.shape  # prefill: (B, Seq_Len, Dim); decode: (B, 1, Dim)
        x = x.view(-1, self.hidden_size)
        
        # 1. 计算 Q K V 并且 reshape 它们尺寸, 方便后续做 self-attention
        xq = self.q_proj(x)
        xkv = F.linear(x, self.kv_proj_weight.data) # (B, L, 2 * num_kv_heads * head_dim)
        
        # 2. 应用旋转位置编码到 Q 和 K, 获取 kv 缓冲向量并更新 kv 向量
        xk, xv = torch.split(xkv, self.num_kv_heads * self.head_dim, dim=-1)
        xq = xq.view(batch_size, self.num_q_heads, self.head_dim)
        xk = xk.view(batch_size, self.num_kv_heads, self.head_dim)
        xv = xv.view(batch_size, self.num_kv_heads, self.head_dim)
        
        cos, sin = position_embeddings
        xq, xk = rope_emb_forward(xq, xk, cos, sin, batch_size, seq_len)

        # 3. 完成形状变换, 并更新 kv_buffer, 即类似 torch.concat[past_kv_values, kv_values]
        combined_kv = torch.cat([xk, xv], dim=-2) # (BS, 2*num_kv_heads, head_dim)
        # 更新 kv_buffer, atten_info.kv_buffer[layer_index]
        update_kv_buffer(combined_kv, atten_info.cur_select_index, atten_info.kv_buffer[layer_index])
        
        # 4. flashattention 计算: softmax(qk^t) * v
        output = flash_decoding(
            xq, 
            atten_info.kv_buffer[layer_index][:, : self.num_kv_heads, :], 
            atten_info.kv_buffer[layer_index][:, self.num_kv_heads:, :], 
            qk_scale,
            atten_info.b_req_tokens_table, 
            atten_info.b_seq_len, 
            atten_info.max_actual_seq_len
        ) # ouput shape is [batchs, num_heads, head_dim]; batchs = batch_size(seq_len = 1)
        
        output = output.view(batch_size, seq_len, self.hidden_size)
        output = self.o_proj(output)
        return output

class FusedMLP(nn.Module):

    def __init__(self, config: LlamaConfig):
        super().__init__()
        
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size

        self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False, dtype=torch.float16) # (in, out)
        self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False, dtype=torch.float16) # (in, out)
        self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False, dtype=torch.float16) # (in, out)

    def forward(self, x):
        return self.down_proj(swiglu_forward(self.gate_proj(x), self.up_proj(x))) # (B, S, D)

class LlamaDecoderLayer(nn.Module):

    def __init__(self, config: LlamaConfig):
        super().__init__()
        self.config= config
        self.num_heads = config.num_heads
        self.hidden_size = config.hidden_size
        self.head_dim = config.head_dim if config.head_dim is not None else config.hidden_size // config.num_heads
        self.rmsnorm_eps = config.rms_norm_eps

        self.attention_norm_weight = nn.Parameter(torch.ones(self.hidden_size,), requires_grad=False) # (hidden_size, )
        self.ffn_norm_weight = nn.Parameter(torch.ones(self.hidden_size,), requires_grad=False) # (hidden_size, )
        
        self.self_attn = FusedAttention(config)
        self.mlp = FusedMLP(config)

    def forward(self, 
        hidden_states: torch.Tensor, # (B, S, D)
        atten_info,
        layer_index: int,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None, # (B, S, Hd)
        qk_scale = None,
        residual: Optional[torch.Tensor] = None
    ):
        # Normalization before the attention block.
        _, seq_len, _ = hidden_states.shape # [batch_size, seq_len, hidden_dim]
        
        hidden_states, residual = skip_rmsnorm(hidden_states, residual, self.attention_norm_weight.data, self.rmsnorm_eps) # residual: [batch_size, seq_len, hidden_dim]

        if seq_len > 1:
            hidden_states = self.self_attn.context_forward(
                hidden_states, atten_info, layer_index, position_embeddings, qk_scale
            )
        else:
            hidden_states = self.self_attn.token_forward(
                hidden_states, atten_info, layer_index, position_embeddings, qk_scale
            )

        mask_nan = torch.isnan(hidden_states)
        hidden_states = torch.where(mask_nan, 1e-8, hidden_states)
        mask_nan = torch.isinf(hidden_states)
        hidden_states = torch.where(mask_nan, 1e-8, hidden_states)
        if torch.isnan(hidden_states).any() or torch.isinf(hidden_states).any(): # 检查 NaNs and inf
            raise ValueError(f"NaNs or inf detected in token_forward output at layer {layer_index}")                
        
        hidden_states, residual = skip_rmsnorm(hidden_states, residual, self.ffn_norm_weight.data, self.rmsnorm_eps)
        hidden_states = self.mlp.forward(hidden_states)
        return hidden_states, residual
        

class LlamaModel(nn.Module):
    def __init__(self, config: LlamaConfig):
        super().__init__()

        self.config = config
        self.vocab_size = config.vocab_size
        self.num_layers = config.num_layers
        self.head_dim = config.head_dim if config.head_dim is not None else config.hidden_size // config.num_heads
        self.qk_scale = 1.0 / (self.head_dim ** 0.5)
        self.rmsnorm_eps = config.rms_norm_eps

        # self.hidden_states = []

        self.rotary_emb = LlamaRotaryEmbedding(config=config)
        self.embed_tokens = nn.Embedding(self.vocab_size, config.hidden_size, dtype=torch.float16)
        self.norm_weight = nn.Parameter(torch.ones(config.hidden_size,), requires_grad=False) # output RMSNorm->gamma

        # 使用 nn.Linear 层替代 lm_head_weight
        self.lm_head = nn.Linear(config.hidden_size, self.vocab_size, bias=False, dtype=torch.float16) # (in, out)

        self.layers = nn.ModuleList(
            [LlamaDecoderLayer(config) for _ in range(config.num_layers)]
        )

    def forward(
        self, 
        input_ids: torch.Tensor, # batch_size, seq_len
        position_ids: torch.Tensor, # [batch_size, seq_length]
        atten_info, 
        inputs_embeds: Optional[torch.Tensor] = None, # [B, S, D]
    ):
        # self.hidden_states = []
        batch_size, seq_len = input_ids.shape
        residual = None

        if inputs_embeds is not None: # To support Multi-model Model
            h = inputs_embeds
        else:
            h = self.get_input_embeddings(input_ids) # (B, S, D)

        if seq_len > 1:
            qk_scale = self.qk_scale * 1.4426950408889634
        else:
            qk_scale = self.qk_scale
        
        position_embeddings = self.rotary_emb(h, position_ids) # cos shape is [1, seq_len, head_dim] -> decode: [batch_size, seq_len, head_dim]
        
        for i, layer in enumerate(self.layers): # Consecutively apply all the encoder layers
            # self.hidden_states.append(h)
            h, residual = layer(h, atten_info, i, position_embeddings, qk_scale, residual)  # h.shape [batch_size, seq_len, hidden_dim]

        h, _ = skip_rmsnorm(h, residual, self.norm_weight.data, self.rmsnorm_eps)
        # self.hidden_states.append(h)
        output = self.lm_head(h) # (B, S, D)->(B, S, vocab_size)

        return output
    
    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.embed_tokens(input_ids) # (B, S)->(B, S, D)