# https://github.com/ModelTC/lightllm/blob/main/lightllm/models/llama/triton_kernel/context_flashattention_nopad.py
# https://github.com/ELS-RD/kernl/blob/main/src/kernl/implementations/attention.py#L438

import torch,math
import triton
import triton.language as tl
from torch.amp import custom_fwd
# from torch.cuda.amp import custom_fwd
from typing import List, Optional, Union
import torch.nn.functional as F

configs_tma = [
    triton.Config({'BLOCK_M_SIZE': BM, 'BLOCK_N_SIZE': BN}, num_stages=stages, num_warps=warps) \
    for BM in [16, 32, 64, 128]\
    for BN in [16, 32, 64, 128]\
    for warps in [4, 8, 16]\
    for stages in [2, 3, 4, 6]\
]

def keep_tma(conf):
    BLOCK_M_SIZE = conf.kwargs["BLOCK_M_SIZE"]
    BLOCK_N_SIZE = conf.kwargs["BLOCK_N_SIZE"]
    if (torch.cuda.get_device_capability()[0] == 9 and BLOCK_M_SIZE * BLOCK_N_SIZE < 128 * 128 and conf.num_warps == 8):
        return False
    return True

# 根据key['n_size', 'BLOCK_DHEAD_SIZE'] 参数, 进行BLOCK_M_SIZE, BLOCK_N_SIZE的调优
@triton.autotune(
    configs=list(filter(keep_tma, configs_tma)), 
    key=['m_size', 'BLOCK_DHEAD_SIZE']
)

# TODO: integrating rope with flash-attn
@triton.jit
def flash_attention_v1_kernel(
    q_ptr,
    k_ptr,
    v_ptr,
    o_ptr,

    q_batch_stride,
    q_heads_stride,
    q_seq_stride,
    q_dim_stride,

    k_batch_stride,
    k_heads_stride,
    k_seq_stride,
    k_dim_stride, # matrix Q stride for columns, [seq_len, head_dim]

    v_batch_stride,
    v_heads_stride,
    v_seq_stride,
    v_dim_stride,

    out_batch_stride,
    out_heads_stride,
    out_seq_stride,
    out_dim_stride,

    num_kv_groups, # group of kv heads
    n_heads,      # number of heads
    m_size,
    n_size,       # sequence length of k, also be rows of K matrix

    BLOCK_DHEAD_SIZE: tl.constexpr, # head_dim dimension
    BLOCK_M_SIZE: tl.constexpr, # BLOCK size of m_size dimension，即 Q 矩阵行数分成了m_size // BLOCK_M_SIZE 块，块大小是 BLOCK_M_SIZE, auto tune
    BLOCK_N_SIZE: tl.constexpr, # n_size dimension, auto tune
    sm_scale,
    causal_mask
    ):
    """
    flashattention 内核实现
    """
    block_m_idx = tl.program_id(0)
    head_idx = tl.program_id(1)

    cur_batch_idx = head_idx // n_heads
    cur_head_idx = head_idx % n_heads

    cur_kv_head_idx = cur_head_idx // num_kv_groups

    m_range_offs = tl.arange(0, BLOCK_M_SIZE)
    n_range_offs = tl.arange(0, BLOCK_N_SIZE) # head_dim 维度偏移
    dhead_range_offs = tl.arange(0, BLOCK_DHEAD_SIZE)

    m_offs = block_m_idx * BLOCK_M_SIZE + m_range_offs

    # Compute offsets for the first block on matrix Q K V Output
    q_offs = ( 
        cur_batch_idx * q_batch_stride 
        + cur_head_idx * q_heads_stride
        + (m_offs[:, None] * q_seq_stride + dhead_range_offs[None,:] * q_dim_stride))

    k_offs = (
        cur_batch_idx * k_batch_stride 
        + cur_kv_head_idx * k_heads_stride
        + (n_range_offs[:,None] * k_seq_stride + dhead_range_offs[None,:] * k_dim_stride))
    
    v_offs = ( 
        cur_batch_idx * v_batch_stride 
        + cur_kv_head_idx * v_heads_stride
        + (n_range_offs[:,None] * v_seq_stride + dhead_range_offs[None,:] * v_dim_stride))

    o_offs = ( 
        cur_batch_idx * out_batch_stride 
        + cur_head_idx * out_heads_stride
        + (m_offs[:,None] * out_seq_stride + dhead_range_offs[None,:] * out_dim_stride))
    
    q_ptrs = q_ptr + q_offs
    k_ptrs = k_ptr + k_offs
    v_ptrs = v_ptr + v_offs
    out_ptrs = o_ptr + o_offs

    # 初始化用于计算 softmax 归一化项的 m 和 d, 意义见 online-softmax, 这里
    l_i = tl.zeros((BLOCK_M_SIZE,), dtype=tl.float32) - float("inf")
    d_i = tl.zeros((BLOCK_M_SIZE,), dtype=tl.float32)
    acc = tl.zeros((BLOCK_M_SIZE, BLOCK_DHEAD_SIZE), dtype=tl.float32)
    
    q_mask = m_offs[:, None] < m_size
    q = tl.load(q_ptrs, mask=q_mask, other=0.0)

    for block_n_start_idx in range(0, n_size, BLOCK_N_SIZE):
        block_n_offs = block_n_start_idx + n_range_offs
        k_mask = block_n_offs[:, None] < n_size
        k = tl.load(k_ptrs + block_n_start_idx * k_seq_stride, mask=k_mask, other=0.0)
        
        qk = tl.zeros((BLOCK_M_SIZE, BLOCK_N_SIZE), dtype=tl.float32)
        qk += tl.dot(q, tl.trans(k))
        
        # 应用因果遮罩
        if causal_mask:
            offs_k = block_n_offs
            offs_m = m_offs
            # casual 模型的 causal mask 下三角矩阵
            mask = offs_m[:, None] >= offs_k[None, :]
            # mask = offs_m[:, None] < offs_k[None, :]
            qk = tl.where(mask, qk * sm_scale, -1.0e8)
        else:
            qk = qk * sm_scale

        l_j = tl.max(qk, 1)
        numerators = tl.exp(qk - l_j[:, None])
        d_j = tl.sum(numerators, 1) # 1d vector

        l_new = tl.maximum(l_i, l_j)
        alpha = tl.exp(l_i - l_new)
        beta = tl.exp(l_j - l_new)
        d_new = alpha * d_i  + beta * d_j
        
        # compute softmax(qk)
        p_scale = beta / d_new
        p = numerators * p_scale[:, None]
        # acc scaling
        sigma = d_i / d_new * alpha
        acc = acc * sigma[:, None]
        
        # compute O = PV
        v = tl.load(v_ptrs + block_n_start_idx * v_seq_stride, mask=k_mask, other=0.0)
        p = p.to(q_ptr.dtype.element_ty)

        acc += tl.dot(p, v)

        # update the normalizer (l and d) for next iteration
        l_i = l_new
        d_i = d_new
    
    out_mask = m_offs[:, None] < m_size
    tl.store(out_ptrs, acc, mask=out_mask)

@torch.no_grad()
@custom_fwd(device_type='cuda', cast_inputs=torch.float16)
def flash_attention_v1(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    ):
    """Compute Flash-attention, can't support fp32 input
    参数:
        q: Query tensor, shape: [bs, n_heads, m_size, head_dim], decode 阶段, q 的 seq_len 和 k v 不一致, 其值为 1
        k: Key tensor,  shape: [bs, n_heads, n_size, head_dim]. 
        v: Value tensor, shape is consistent with k. 
        output: Attention ouput tensor, shape is consistent with q. 
        attention_mask: Attention mask matrix broadcastable to (batch, head_size, m_size, n_size).
    """
    num_kv_groups = q.shape[1] // k.shape[1] # num_q_heads // num_k_heads
    output = torch.empty_like(q)
    assert q.device.type == 'cuda', "Input tensor q must be on CUDA device"
    assert k.device.type == 'cuda', "Input tensor keys must be on CUDA device"

    assert q.shape[-1] == k.shape[-1] == v.shape[-1]
    assert (
            q.dtype == k.dtype == v.dtype == output.dtype
        ), f"All tensors must have the same dtype: {q.dtype}, {k.dtype}, {v.dtype}, {output.dtype}"
    
    # sequence length of q, also be rows of Q matrix
    bs, n_heads, m_size, HEAD_DIM = q.size()
    causal_mask = False
    if m_size > 1:
        causal_mask: bool = True
        
    n_size = k.shape[2]
    sm_scale = 1 / math.sqrt(HEAD_DIM)
    # BLOCK_M_SIZE = 128
    grid = lambda meta: (triton.cdiv(m_size, meta["BLOCK_M_SIZE"]), bs*n_heads, 1) # 二维 grid

    flash_attention_v1_kernel[grid](
        q,
        k,
        v, 
        output,
        *q.stride(),  # (batch, heads, m_size, head_dim)
        *k.stride(),  # (batch, heads, n_size, head_dim)
        *v.stride(),  # (batch, heads, n_size, head_dim)
        *output.stride(),  # (batch, heads, m_size, n_size)

        num_kv_groups,
        n_heads,
        m_size,
        n_size,
        
        HEAD_DIM,
        sm_scale=sm_scale,
        causal_mask=causal_mask
    )
    return output

def standard_attention(Q, K, V, sm_scale, mask=None):
    """
    标准的 PyTorch 实现的自注意力机制。
    
    Args:
        Q (torch.Tensor): 查询张量，形状 (batch_size, num_heads, seq_length, head_dim)
        K (torch.Tensor): 键张量，形状 (batch_size, num_heads, seq_length, head_dim)
        V (torch.Tensor): 值张量，形状 (batch_size, num_heads, seq_length, head_dim)
        sm_scale (float): Softmax 缩放因子
        mask (torch.Tensor, optional): 遮罩张量，形状 (batch_size, num_heads, seq_length, seq_length)
    
    Returns:
        torch.Tensor: 注意力输出，形状与 Q 相同
    """
    # 计算 QK^T
    attn_scores = torch.matmul(Q, K.transpose(-2, -1)) * sm_scale  # (batch_size, num_heads, seq_length, seq_length)
    
    if mask is not None:
        attn_scores = attn_scores.masked_fill(mask == 0, float('-inf'))
    
    # print("attn_scores", attn_scores)
    attn_weights = F.softmax(attn_scores, dim=-1)
    
    # 计算注意力输出
    out = torch.matmul(attn_weights, V)  # (batch_size, num_heads, seq_length, head_dim)
    
    return out

def _sdpa(q, k, v):
    # Args:
    #     Q (torch.Tensor): 查询张量，形状 (batch_size, num_heads, seq_length, head_dim)
    #     K (torch.Tensor): 键张量，形状 (batch_size, num_heads, seq_length, head_dim)
    #     V (torch.Tensor): 值张量，形状 (batch_size, num_heads, seq_length, head_dim)
    output = torch.nn.functional.scaled_dot_product_attention(q, k, v, is_causal=True)

    return output


def test_prefill_state(batch=4, n_heads=32, head_dim=128, max_seq_len_list=[1024, 2048, 4096]):
    import matplotlib.pyplot as plt
    device = "cuda"
    sm_scale = 1.0 / math.sqrt(head_dim)
    max_seq_len = max_seq_len_list[0]
    
    # q shape: [bs, n_heads, seq_len, head_dim] (decode 阶段 q 的 seq_len=1)
    # k shape: [bs, n_heads, seq_len, head_dim]
    shape = (batch, n_heads, max_seq_len, head_dim)
    q = torch.randn(shape, device=device, dtype=torch.float16)
    k = torch.randn(shape, device=device, dtype=torch.float16)
    v = torch.randn(shape, device=device, dtype=torch.float16)
    
    triton_output = flash_attention_v1(q, k, v)
    # 创建mask
    mask = torch.tril(torch.ones((max_seq_len, max_seq_len))).unsqueeze(0).unsqueeze(0).type_as(q)  # (1, 1, seq, seq)
    torch_output = standard_attention(q, k, v, sm_scale, mask)
    print(f'The maximum difference between torch and triton is {torch.max(torch.abs(torch_output - triton_output))}')
    
    # =============================================================================
    # 2, 内核运行速度性能比较
    # =============================================================================
    flash_times = []
    standard_times = []
    iterations = 50

    for seq_len in max_seq_len_list:
        # q 的 shape: [batch, n_heads, head_dim] (decode 阶段 q 的 seq_len=1)
        shape = (batch, n_heads, seq_len, head_dim)
        q = torch.randn(shape, device=device, dtype=torch.float16)
        k = torch.randn(shape, device=device, dtype=torch.float16)
        v = torch.randn(shape, device=device, dtype=torch.float16)
    
        # 预热
        _ = flash_attention_v1(q, k, v)
        torch.cuda.synchronize()
        start_event = torch.cuda.Event(enable_timing=True)
        end_event = torch.cuda.Event(enable_timing=True)
        start_event.record()
        for _ in range(iterations):
            _ = flash_attention_v1(q, k, v)
        end_event.record()
        torch.cuda.synchronize()
        flash_time = start_event.elapsed_time(end_event) / iterations
        flash_times.append(flash_time)

        # 标准 attention 预热
        mask = torch.tril(torch.ones((seq_len, seq_len))).unsqueeze(0).unsqueeze(0).type_as(q)  # (1, 1, seq, seq)
        _ = _sdpa(q, k, v)
        torch.cuda.synchronize()
        start_event.record()
        for _ in range(iterations):
            _ = _sdpa(q, k, v)
        end_event.record()
        torch.cuda.synchronize()
        standard_time = start_event.elapsed_time(end_event) / iterations
        standard_times.append(standard_time)

        print(f"max_seq_len = {seq_len:4d}: flash_attn = {flash_time:.3f} ms, standard_attn = {standard_time:.3f} ms")
    # 绘制性能对比曲线
    plt.figure(figsize=(8, 5))
    plt.plot(max_seq_len_list, flash_times, marker='o', label="Flash Attentionv1")
    plt.plot(max_seq_len_list, standard_times, marker='s', label="Standard Attention")
    plt.xlabel("max_seq_len (kv cache length)")
    plt.ylabel("Average execution time (ms)")
    plt.title("Prefill Stage Performance Comparison")
    plt.legend()
    plt.grid(True)
    plt.savefig("./images/flashattentionv1_benchamrk.png")

    return {
        "max_seq_len_list": max_seq_len_list,
        "flash_times": flash_times,
        "standard_times": standard_times,
    }


def test_decode_stage():
    # 设置测试参数
    batch_size = 1
    num_heads = 4
    initial_seq_length = 16
    generated_seq_length = 16
    head_dim = 64
    BLOCK_M = 16
    BLOCK_N = 16

    # 生成固定的初始输入张量
    torch.manual_seed(0)
    q_initial = torch.randn(batch_size, num_heads, initial_seq_length, head_dim, device='cuda', dtype=torch.float32)
    k_initial = torch.randn(batch_size, num_heads, initial_seq_length, head_dim, device='cuda', dtype=torch.float32)
    v_initial = torch.randn(batch_size, num_heads, initial_seq_length, head_dim, device='cuda', dtype=torch.float32)
    o_initial = torch.zeros_like(q_initial, device='cuda', dtype=torch.float32)
    new_token_q = torch.randn(batch_size, num_heads, 1, head_dim, device='cuda', dtype=torch.float32)

    triton_k_extended = k_initial
    triton_v_extended = v_initial
    torch_k_extended = k_initial
    torch_v_extended = v_initial
    torch_new_token_q = new_token_q
    triton_new_token_q = new_token_q
    # 模拟生成过程中逐步增加序列长度
    for step in range(1, generated_seq_length + 1):
        # 生成新的 token
        triton_k_extended = torch.cat([triton_k_extended, triton_new_token_q], dim=2)
        triton_v_extended = torch.cat([triton_v_extended, triton_new_token_q], dim=2)
        
        torch_k_extended = torch.cat([torch_k_extended, torch_new_token_q], dim=2)
        torch_v_extended = torch.cat([torch_v_extended, torch_new_token_q], dim=2)

        # 扩展 Q, K, V 和 Out
        # q_extended = torch.cat([q_initial, new_token_q], dim=2)

        # 计算 Softmax 缩放因子, sm_scale * 1.4426950408889634 精度可控制在 1e-2 内
        sm_scale_extended = 1.0 / math.sqrt(head_dim)

        # 计算 Triton 内核输出
        triton_new_token_q = flash_attention_v1(new_token_q, triton_k_extended, triton_v_extended)

        # 使用标准 PyTorch 实现计算扩展后的注意力输出
        torch_new_token_q = standard_attention(new_token_q, torch_k_extended, torch_v_extended, sm_scale_extended)

        # 比较 Triton 内核输出与标准实现的输出
        if torch.allclose(triton_new_token_q, torch_new_token_q, atol=1e-1):
            print(f"Decode Stage Step {step} Test Passed: Triton output matches PyTorch standard implementation.")
        else:
            max_diff = (triton_new_token_q - torch_new_token_q).abs().max()
            print(f"Decode Stage Step {step} Test Failed: Maximum difference {max_diff}")
            # 可选择打印更多信息进行调试
            break  # 根据需要是否停止测试

if __name__ == "__main__":
    print("Running Prefill Stage Test...")
    stats = test_prefill_state()
    print("prefill Benchmark statistics:", stats, "\n")
    print("\nRunning Decode Stage Test...")
    test_decode_stage()

"""
Running Prefill Stage Test...
The maximum difference between torch and triton is 0.00390625
best config selected: BLOCK_M_SIZE: 128, BLOCK_N_SIZE: 64, num_warps: 16, num_ctas: 1, num_stages: 2
max_seq_len = 1024: flash_attn = 5.348 ms, standard_attn = 1.853 ms
max_seq_len = 2048: flash_attn = 13.953 ms, standard_attn = 5.810 ms
max_seq_len = 4096: flash_attn = 82.544 ms, standard_attn = 22.517 ms

Running Decode Stage Test...
Decode Stage Step 1 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 2 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 3 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 4 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 5 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 6 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 7 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 8 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 9 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 10 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 11 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 12 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 13 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 14 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 15 Test Passed: Triton output matches PyTorch standard implementation.
Decode Stage Step 16 Test Passed: Triton output matches PyTorch standard implementation.
"""