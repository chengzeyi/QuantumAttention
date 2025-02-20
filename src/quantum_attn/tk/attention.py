import functools
import os

import torch
from torch.utils.cpp_extension import load_inline

from . import utils

TK_ATTENTION_SOURCE = """
#include "kittens.cuh"
#include <cooperative_groups.h>
#include <iostream>

using namespace kittens;
namespace cg = cooperative_groups;

#if defined(TK_ATTN_DTYPE_FP16)
typedef half DType;
#elif defined(TK_ATTN_DTYPE_BF16)
typedef bf16 DType;
#else
#error "Unsupported dtype"
#endif

#if defined(TK_ATTN_IS_FP8)
typedef fp8e4m3 QKDType;
#else
typedef DType QKDType;
#endif

__device__ static inline float fast_exp2f(float x) {
    float y;
    asm volatile ( "ex2.approx.ftz.f32 %0, %1; " : "=f"(y) : "f"(x));
    return y;
}

namespace kittens {
namespace base_ops {

struct fast_exp2 {
    template<typename T> static __device__ inline T op(const T &x) { return fast_exp2f(x); }
};
template<> __device__ inline float  fast_exp2::op<float> (const float &x ) { return fast_exp2f(x);                        }
template<> __device__ inline float2 fast_exp2::op<float2>(const float2 &x) { return float2{fast_exp2f(x.x), fast_exp2f(x.y)}; }

}
}

template<int D> struct fwd_attend_ker_tile_dims {};
template<> struct fwd_attend_ker_tile_dims<64> {
    constexpr static int tile_width = (64);
    constexpr static int qo_height  = (4*16);
    constexpr static int kv_height  = (8*16);
    constexpr static int stages     = (2);
};
template<> struct fwd_attend_ker_tile_dims<128> {
    constexpr static int tile_width = (128);
    constexpr static int qo_height  = (4*16);
    constexpr static int kv_height  = (8*16);
    constexpr static int stages     = (2);
};
template<> struct fwd_attend_ker_tile_dims<256> {
    constexpr static int tile_width = (256);
    constexpr static int qo_height  = (4*16);
    constexpr static int kv_height  = (4*16);
    constexpr static int stages     = (2);
};

template<int D> struct fwd_globals {
    using q_tile    =         st<QKDType, fwd_attend_ker_tile_dims<D>::qo_height, fwd_attend_ker_tile_dims<D>::tile_width>;
    using k_tile    =         st<QKDType, fwd_attend_ker_tile_dims<D>::kv_height, fwd_attend_ker_tile_dims<D>::tile_width>;
    using v_tile    =         st<DType, fwd_attend_ker_tile_dims<D>::kv_height, fwd_attend_ker_tile_dims<D>::tile_width>;
    // using l_col_vec = col_vec<st_fl<fwd_attend_ker_tile_dims<D>::qo_height, fwd_attend_ker_tile_dims<D>::tile_width>>;
    using o_tile    =         st<DType, fwd_attend_ker_tile_dims<D>::qo_height, fwd_attend_ker_tile_dims<D>::tile_width>;

    using q_gl = gl<QKDType,  -1, -1, -1, D, q_tile>;
    using k_gl = gl<QKDType,  -1, -1, -1, D, k_tile>;
    using v_gl = gl<DType,  -1, -1, -1, D, v_tile>;
    // using l_gl = gl<float, -1, -1, 1, -1, l_col_vec>;
    using o_gl = gl<DType,  -1, -1, -1, D, o_tile>;

    q_gl q;
    k_gl k;
    v_gl v;
    // l_gl l;
    o_gl o;

#if defined(TK_ATTN_IS_FP8)
    float* scale_q;
    float* scale_k;
#endif

    const int N;
    const int hr;
};

template<int D, bool is_causal, int CONSUMER_WARPGROUPS, int PRODUCER_WARPGROUPS>
__global__  __launch_bounds__(((CONSUMER_WARPGROUPS+PRODUCER_WARPGROUPS)*kittens::WARPGROUP_WARPS)*kittens::WARP_THREADS, 1)
void fwd_attend_ker(const __grid_constant__ fwd_globals<D> g) {
    constexpr int NUM_WARPGROUPS      = (CONSUMER_WARPGROUPS+PRODUCER_WARPGROUPS);
    constexpr int NUM_WORKERS         = (NUM_WARPGROUPS*kittens::WARPGROUP_WARPS);

    extern __shared__ int __shm[];
    tma_swizzle_allocator al((int*)&__shm[0]);
    int warpid = kittens::warpid(), warpgroupid = warpid/kittens::WARPGROUP_WARPS;

    using K = fwd_attend_ker_tile_dims<D>;

    using q_tile    =         st<QKDType, K::qo_height, K::tile_width>;
    using k_tile    =         st<QKDType, K::kv_height, K::tile_width>;
    using v_tile    =         st<DType, K::kv_height, K::tile_width>;
    // using l_col_vec = col_vec<st_fl<K::qo_height, K::tile_width>>;
    using o_tile    =         st<DType, K::qo_height, K::tile_width>;

    union TileUnion {
        q_tile q;
        o_tile o;
    };

    TileUnion (&q_o_smem)[CONSUMER_WARPGROUPS] = al.allocate<TileUnion, CONSUMER_WARPGROUPS>();
    k_tile    (&k_smem)[K::stages]           = al.allocate<k_tile, K::stages          >();
    v_tile    (&v_smem)[K::stages]           = al.allocate<v_tile, K::stages          >();
    // l_col_vec (&l_smem)[CONSUMER_WARPGROUPS] = al.allocate<l_col_vec, CONSUMER_WARPGROUPS>();

    int kv_blocks   = (g.N + K::kv_height - 1) / (K::kv_height);
    int kv_head_idx = blockIdx.y / g.hr;
    int seq_idx     = blockIdx.x * CONSUMER_WARPGROUPS;

    __shared__ kittens::semaphore qsmem_semaphore, k_smem_arrived[K::stages], v_smem_arrived[K::stages], compute_done[K::stages], qk_done[K::stages];
    if (threadIdx.x == 0) {
        init_semaphore(qsmem_semaphore, 0, 1);
        for(int j = 0; j < K::stages; j++) {
            init_semaphore(k_smem_arrived[j], 0, 1);
            init_semaphore(v_smem_arrived[j], 0, 1);
            if constexpr (D >= 128) {
                init_semaphore(qk_done[j], CONSUMER_WARPGROUPS, 0);
            }
            init_semaphore(compute_done[j], CONSUMER_WARPGROUPS, 0);
        }

        tma::expect_bytes(qsmem_semaphore, sizeof(q_tile) * CONSUMER_WARPGROUPS);

        for (int wg = 0; wg < CONSUMER_WARPGROUPS; wg++) {
            coord<q_tile> q_tile_idx = {blockIdx.z, blockIdx.y, (seq_idx) + wg, 0};
            tma::load_async(q_o_smem[wg].q, g.q, q_tile_idx, qsmem_semaphore);
        }

        for (int j = 0; j < K::stages - 1; j++) {
            coord<k_tile> kv_tile_idx = {blockIdx.z, kv_head_idx, j, 0};
            tma::expect_bytes(k_smem_arrived[j], sizeof(k_tile));
            tma::load_async(k_smem[j], g.k, kv_tile_idx, k_smem_arrived[j]);
            tma::expect_bytes(v_smem_arrived[j], sizeof(v_tile));
            tma::load_async(v_smem[j], g.v, kv_tile_idx, v_smem_arrived[j]);
        }
    }
    __syncthreads();

    int pipe_idx = K::stages - 1;

    if(warpgroupid == NUM_WARPGROUPS-1) {
        // warpgroup::decrease_registers<32>();
        warpgroup::producer_registers();

        int kv_iters;
        if constexpr (is_causal) {
            kv_iters = (seq_idx * K::qo_height) - 1 + (CONSUMER_WARPGROUPS * K::qo_height);
            kv_iters = ((kv_iters / K::kv_height) == 0) ? (0) : ((kv_iters / K::kv_height) - 1);
        }
        else { kv_iters = kv_blocks-2; }

        if(warpid == NUM_WORKERS-4) {
            for (auto kv_idx = pipe_idx - 1; kv_idx <= kv_iters; kv_idx++) {
                coord<k_tile> kv_tile_idx = {blockIdx.z, kv_head_idx, kv_idx+1, 0};
                if constexpr (D >= 128) {
                    if (kv_idx >= pipe_idx) {
                        wait(qk_done[(kv_idx-pipe_idx)%K::stages], ((kv_idx-(pipe_idx))/K::stages)%2);
                    }
                } else {
                    if (kv_idx >= pipe_idx) {
                        wait(compute_done[(kv_idx-pipe_idx)%K::stages], ((kv_idx-(pipe_idx))/K::stages)%2);
                    }
                }
                tma::expect_bytes(k_smem_arrived[(kv_idx+1)%K::stages], sizeof(k_tile));
                tma::load_async(k_smem[(kv_idx+1)%K::stages], g.k, kv_tile_idx, k_smem_arrived[(kv_idx+1)%K::stages]);
                if constexpr (D >= 128) {
                    if (kv_idx >= pipe_idx) {
                        wait(compute_done[(kv_idx-pipe_idx)%K::stages], ((kv_idx-(pipe_idx))/K::stages)%2);
                    }
                }
                tma::expect_bytes(v_smem_arrived[(kv_idx+1)%K::stages], sizeof(v_tile));
                tma::load_async(v_smem[(kv_idx+1)%K::stages], g.v, kv_tile_idx, v_smem_arrived[(kv_idx+1)%K::stages]);
            }
        }
    }
    else {
        // warpgroup::increase_registers<160>();
        warpgroup::consumer_registers<CONSUMER_WARPGROUPS>();

        rt_fl<16, K::tile_width> o_reg;

#if defined(TK_ATTN_IS_FP8)
        col_vec<rt_fl<16, K::kv_height>> max_vec, norm_vec, max_vec_last;

        float scale_q = g.scale_q[blockIdx.z*gridDim.y + blockIdx.y];
        float scale_k = g.scale_k[blockIdx.z*gridDim.y + blockIdx.y];

        float scale = scale_q * scale_k;
        if constexpr (D == 64)       { scale *= 1.44269504089f*0.125f; }
        else if constexpr (D == 128) { scale *= 1.44269504089f*0.08838834764f; }
        else                         { scale *= 1.44269504089f*0.0625f; }
#else
        col_vec<rt_fl<16, K::kv_height>> max_vec, norm_vec, max_vec_last_scaled;
#endif

        neg_infty(max_vec);
        zero(norm_vec);
        zero(o_reg);

        int kv_iters;
        if constexpr (is_causal) {
            kv_iters = (seq_idx * K::qo_height) - 1 + (CONSUMER_WARPGROUPS * K::qo_height);
            kv_iters = (kv_iters / K::kv_height);
        }
        else { kv_iters = kv_blocks - 1; }

        wait(qsmem_semaphore, 0);

        for (auto kv_idx = 0; kv_idx <= kv_iters; kv_idx++) {
            rt_fl<16, K::kv_height>  att_block;
            rt<DType, 16, K::kv_height>  att_block_mma;

            wait(k_smem_arrived[(kv_idx)%K::stages], (kv_idx/K::stages)%2);
            warpgroup::mm_ABt(att_block, q_o_smem[warpgroupid].q, k_smem[(kv_idx)%K::stages]);

#if defined(TK_ATTN_IS_FP8)
            copy(max_vec_last, max_vec);
#else
            if constexpr (D == 64)       { mul(max_vec_last_scaled, max_vec, 1.44269504089f*0.125f); }
            else if constexpr (D == 128) { mul(max_vec_last_scaled, max_vec, 1.44269504089f*0.08838834764f); }
            else                         { mul(max_vec_last_scaled, max_vec, 1.44269504089f*0.0625f); }
#endif

            warpgroup::mma_async_wait();
            if constexpr (D >= 128) {
                if(warpgroup::laneid() == 0) arrive(qk_done[(kv_idx)%K::stages], 1);
            }

#if defined(TK_ATTN_IS_FP8)
            mul(att_block, att_block, scale);
#endif

            if constexpr (is_causal) {
                if (kv_idx == kv_iters-1 || kv_idx == kv_iters) {
                    const int q_blk = (seq_idx * (K::qo_height/kittens::TILE_ROW_DIM<QKDType>)) + warpid;
                        int k_blk = (kv_idx * (K::kv_height/kittens::TILE_ROW_DIM<QKDType>));

                    #pragma unroll
                    for (auto j = 0; j < (K::kv_height/kittens::TILE_ROW_DIM<QKDType>); j++) {
                        auto k_idx = k_blk + j;
                        auto &attn_subtile = reinterpret_cast<rt_fl<16, 16>&>(att_block.tiles[0][j]);

                        if      (k_idx >  q_blk) { neg_infty  (attn_subtile); }
                        else if (k_idx == q_blk) { make_causal(attn_subtile, attn_subtile, kittens::base_types::constants<float>::neg_infty()); }
                        __syncwarp();
                    }
                }
            }
            else {
                if (kv_idx == kv_iters && g.N % K::kv_height != 0) {
                    right_fill(att_block, att_block, g.N % K::kv_height, kittens::base_types::constants<float>::neg_infty());
                }
            }

            row_max(max_vec, att_block, max_vec);

#if defined(TK_ATTN_IS_FP8)
            sub_row(att_block, att_block, max_vec);
            exp2(att_block, att_block);
            // unary_map<base_ops::fast_exp2>(att_block, att_block);
            sub(max_vec_last, max_vec_last, max_vec);
            exp2(max_vec_last,       max_vec_last);
            // unary_op<base_ops::fast_exp2>(max_vec_last, max_vec_last);
            mul(norm_vec,            norm_vec,     max_vec_last);
            row_sum(norm_vec,  att_block, norm_vec);
            // add(att_block, att_block, 0.f);
            copy(att_block_mma, att_block);
            mul_row(o_reg, o_reg, max_vec_last);
#else
            col_vec<rt_fl<16, K::kv_height>> max_vec_scaled;
            if constexpr (D == 64) {
                mul(att_block, att_block,    1.44269504089f*0.125f);
                mul(max_vec_scaled, max_vec, 1.44269504089f*0.125f);
            }
            else if constexpr (D == 128) {
                mul(att_block, att_block,    1.44269504089f*0.08838834764f);
                mul(max_vec_scaled, max_vec, 1.44269504089f*0.08838834764f);
            }
            else {
                mul(att_block, att_block,    1.44269504089f*0.0625f);
                mul(max_vec_scaled, max_vec, 1.44269504089f*0.0625f);
            }

            sub_row(att_block, att_block, max_vec_scaled);
            exp2(att_block, att_block);
            // unary_map<base_ops::fast_exp2>(att_block, att_block);
            sub(max_vec_last_scaled, max_vec_last_scaled, max_vec_scaled);
            exp2(max_vec_last_scaled,       max_vec_last_scaled);
            // unary_op<base_ops::fast_exp2>(max_vec_last_scaled, max_vec_last_scaled);
            mul(norm_vec,            norm_vec,     max_vec_last_scaled);
            row_sum(norm_vec,  att_block, norm_vec);
            // add(att_block, att_block, 0.f);
            copy(att_block_mma, att_block);
            mul_row(o_reg, o_reg, max_vec_last_scaled);
#endif

            wait(v_smem_arrived[(kv_idx)%K::stages], (kv_idx/K::stages)%2);

            warpgroup::mma_AB(o_reg, att_block_mma, v_smem[(kv_idx)%K::stages]);
            warpgroup::mma_async_wait();

            if(warpgroup::laneid() == 0) arrive(compute_done[(kv_idx)%K::stages], 1);
        }

        div_row(o_reg, o_reg, norm_vec);
        warpgroup::store(q_o_smem[warpgroupid].o, o_reg);
        warpgroup::sync(warpgroupid+4);

        if (warpid % 4 == 0) {
            coord<o_tile> o_tile_idx = {blockIdx.z, blockIdx.y, (seq_idx) + warpgroupid, 0};
            tma::store_async(g.o, q_o_smem[warpgroupid].o, o_tile_idx);
        }

        // mul(max_vec_scaled,   max_vec_scaled, 0.69314718056f);
        // log(norm_vec, norm_vec);
        // add(norm_vec, norm_vec, max_vec_scaled);

        // if constexpr (D == 64) { mul(norm_vec, norm_vec, -8.0f); }
        // else                   { mul(norm_vec, norm_vec, -11.313708499f); }

        // warpgroup::store(l_smem[warpgroupid], norm_vec);
        // warpgroup::sync(warpgroupid+4);

        // if (warpid % 4 == 0) {
        //     coord<l_col_vec> tile_idx = {blockIdx.z, blockIdx.y, 0, (seq_idx) + warpgroupid};
        //     tma::store_async(g.l, l_smem[warpgroupid], tile_idx);
        // }
        tma::store_async_wait();
    }
}

#include "pyutils/torch_helpers.cuh"
#include <ATen/cuda/CUDAContext.h>
#include <iostream>

std::vector<torch::Tensor>
#if TK_ATTN_IS_FP8
attention_forward(const torch::Tensor &q, const torch::Tensor &k, const torch::Tensor &v, const torch::Tensor &scale_q, const torch::Tensor &scale_k, bool causal)
#else
attention_forward(const torch::Tensor &q, const torch::Tensor &k, const torch::Tensor &v, bool causal)
#endif
{
    CHECK_CUDA(q);
    CHECK_CUDA(k);
    CHECK_CUDA(v);
#if TK_ATTN_IS_FP8
    CHECK_CUDA(scale_q);
    CHECK_CUDA(scale_k);
#endif

    TORCH_CHECK(q.device() == k.device(), "Q and K tensors must be on the same device");
    TORCH_CHECK(q.device() == v.device(), "Q and V tensors must be on the same device");

    TORCH_CHECK(q.dim() == 4, "Q tensor must have 4 dimensions");
    TORCH_CHECK(k.dim() == 4, "K tensor must have 4 dimensions");
    TORCH_CHECK(v.dim() == 4, "V tensor must have 4 dimensions");

    auto batch    = q.size(0);
    auto seq_len_q  = q.size(2);
    auto seq_len_kv = k.size(2);
    auto head_dim = q.size(3);
    auto is_causal = causal;
    auto qo_heads = q.size(1);
    auto kv_heads = k.size(1);

    // check to see that these dimensions match for all inputs
    TORCH_CHECK(q.size(0) == batch, "Q batch dimension - idx 0 - must match for all inputs");
    TORCH_CHECK(k.size(0) == batch, "K batch dimension - idx 0 - must match for all inputs");
    TORCH_CHECK(v.size(0) == batch, "V batch dimension - idx 0 - must match for all inputs");

    TORCH_CHECK(q.size(2) == seq_len_q, "Q sequence length dimension - idx 2 - must match for all inputs");
    TORCH_CHECK(k.size(2) == seq_len_kv, "K sequence length dimension - idx 2 - must match for all inputs");
    TORCH_CHECK(v.size(2) == seq_len_kv, "V sequence length dimension - idx 2 - must match for all inputs");

    TORCH_CHECK(q.size(3) == head_dim, "Q head dimension - idx 3 - must match for all non-vector inputs");
    TORCH_CHECK(k.size(3) == head_dim, "K head dimension - idx 3 - must match for all non-vector inputs");
    TORCH_CHECK(v.size(3) == head_dim, "V head dimension - idx 3 - must match for all non-vector inputs");

    TORCH_CHECK(qo_heads >= kv_heads, "QO heads must be greater than or equal to KV heads");
    TORCH_CHECK(qo_heads % kv_heads == 0, "QO heads must be divisible by KV heads");
    TORCH_CHECK(q.size(1) == qo_heads, "QO head dimension - idx 1 - must match for all inputs");
    TORCH_CHECK(k.size(1) == kv_heads, "KV head dimension - idx 1 - must match for all inputs");
    TORCH_CHECK(v.size(1) == kv_heads, "KV head dimension - idx 1 - must match for all inputs");

#if TK_ATTN_IS_FP8
    TORCH_CHECK(scale_q.dtype() == torch::kFloat32, "Q scale tensor must be of type float32");
    TORCH_CHECK(scale_k.dtype() == torch::kFloat32, "K scale tensor must be of type float32");

    TORCH_CHECK(scale_q.dim() == 2, "Q scale tensor must have 2 dimensions");
    TORCH_CHECK(scale_k.dim() == 2, "K scale tensor must have 2 dimensions");

    TORCH_CHECK(scale_q.size(0) == batch, "Q scale batch dimension - idx 0 - must match for all inputs");
    TORCH_CHECK(scale_k.size(0) == batch, "K scale batch dimension - idx 0 - must match for all inputs");
    TORCH_CHECK(scale_q.size(1) == qo_heads, "Q scale head dimension - idx 1 - must match for all inputs");
    TORCH_CHECK(scale_k.size(1) == kv_heads, "K scale head dimension - idx 1 - must match for all inputs");
#endif

    torch::DeviceGuard device_guard(q.device());

    torch:: Tensor q_ = q.contiguous();
    torch:: Tensor k_ = k.contiguous();
    torch:: Tensor v_ = v.contiguous();

    auto hr = qo_heads / kv_heads;

    void* q_ptr = q_.data_ptr();
    void* k_ptr = k_.data_ptr();
    void* v_ptr = v_.data_ptr();

    QKDType*  d_q = reinterpret_cast<QKDType*>(q_ptr);
    QKDType*  d_k = reinterpret_cast<QKDType*>(k_ptr);
    DType*  d_v = reinterpret_cast<DType*>(v_ptr);

    // for the returned outputs
    torch::Tensor o     = torch::empty({static_cast<uint>(batch),
                                        static_cast<uint>(qo_heads),
                                        static_cast<uint>(seq_len_q),
                                        static_cast<uint>(head_dim)}, v.options().memory_format(at::MemoryFormat::Contiguous));

    // auto l_vec_stride_h = (seq_len_q * sizeof(float) + 15) / 16 * 16 / sizeof(float);
    // torch::Tensor l_vec = torch::empty_strided({static_cast<uint>(batch),
    //                                             static_cast<uint>(qo_heads),
    //                                             static_cast<uint>(seq_len_q)},
    //                                            {static_cast<uint>(qo_heads * l_vec_stride_h),
    //                                             static_cast<uint>(l_vec_stride_h),
    //                                             1},
    //                                            torch::dtype(torch::kFloat).device(q.device()));

    DType*  o_ptr = reinterpret_cast<DType*>(o.data_ptr());
    DType*  d_o   = reinterpret_cast<DType*>(o_ptr);

    // float* l_ptr = reinterpret_cast<float*>(l_vec.data_ptr<float>());
    // float* d_l   = reinterpret_cast<float*>(l_ptr);

#if TK_ATTN_IS_FP8
    torch::Tensor scale_q_ = scale_q.contiguous();
    torch::Tensor scale_k_ = scale_k.contiguous();

    void* scale_q_ptr = scale_q_.data_ptr();
    void* scale_k_ptr = scale_k_.data_ptr();

    float* d_scale_q = reinterpret_cast<float*>(scale_q_ptr);
    float* d_scale_k = reinterpret_cast<float*>(scale_k_ptr);
#endif

    auto stream = at::cuda::getCurrentCUDAStream().stream();

    if (head_dim == 64) {
        constexpr int CONSUMER_WARPGROUPS = (3);
        constexpr int PRODUCER_WARPGROUPS = (1);
        constexpr int NUM_WARPGROUPS      = (CONSUMER_WARPGROUPS+PRODUCER_WARPGROUPS);
        constexpr int NUM_WORKERS         = (NUM_WARPGROUPS*kittens::WARPGROUP_WARPS);

        using q_tile    =         st<QKDType, fwd_attend_ker_tile_dims<64>::qo_height, fwd_attend_ker_tile_dims<64>::tile_width>;
        using k_tile    =         st<QKDType, fwd_attend_ker_tile_dims<64>::kv_height, fwd_attend_ker_tile_dims<64>::tile_width>;
        using v_tile    =         st<DType, fwd_attend_ker_tile_dims<64>::kv_height, fwd_attend_ker_tile_dims<64>::tile_width>;
        // using l_col_vec = col_vec<st_fl<fwd_attend_ker_tile_dims<64>::qo_height, fwd_attend_ker_tile_dims<64>::tile_width>>;
        using o_tile    =         st<DType, fwd_attend_ker_tile_dims<64>::qo_height, fwd_attend_ker_tile_dims<64>::tile_width>;

        using q_global = gl<QKDType,  -1, -1, -1, 64, q_tile>;
        using k_global = gl<QKDType,  -1, -1, -1, 64, k_tile>;
        using v_global = gl<DType,  -1, -1, -1, 64, v_tile>;
        // using l_global = gl<float, -1, -1, 1, -1, l_col_vec>;
        using o_global = gl<DType,  -1, -1, -1, 64, o_tile>;

        using globals      = fwd_globals<64>;

        q_global qg_arg{d_q, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};
        k_global kg_arg{d_k, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        v_global vg_arg{d_v, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        // l_global lg_arg{d_l, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), nullptr,  static_cast<unsigned int>(l_vec_stride_h)};
        o_global og_arg{d_o, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};

#if TK_ATTN_IS_FP8
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, d_scale_q, d_scale_k, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#else
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#endif

        auto mem_size = kittens::MAX_SHARED_MEMORY;
        // auto threads  = NUM_WORKERS * kittens::WARP_THREADS;

        constexpr int block_size_m = CONSUMER_WARPGROUPS*fwd_attend_ker_tile_dims<64>::qo_height;
        int num_m_blocks = (seq_len_q + block_size_m - 1) / block_size_m;
        dim3 grid(num_m_blocks, qo_heads, batch);

        if (is_causal) {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<64, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<64, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
        else {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<64, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<64, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
    }

    if (head_dim == 128) {
        constexpr int CONSUMER_WARPGROUPS = (3);
        constexpr int PRODUCER_WARPGROUPS = (1);
        constexpr int NUM_WARPGROUPS      = (CONSUMER_WARPGROUPS+PRODUCER_WARPGROUPS);
        constexpr int NUM_WORKERS         = (NUM_WARPGROUPS*kittens::WARPGROUP_WARPS);

        using q_tile    =         st<QKDType, fwd_attend_ker_tile_dims<128>::qo_height, fwd_attend_ker_tile_dims<128>::tile_width>;
        using k_tile    =         st<QKDType, fwd_attend_ker_tile_dims<128>::kv_height, fwd_attend_ker_tile_dims<128>::tile_width>;
        using v_tile    =         st<DType, fwd_attend_ker_tile_dims<128>::kv_height, fwd_attend_ker_tile_dims<128>::tile_width>;
        // using l_col_vec = col_vec<st_fl<fwd_attend_ker_tile_dims<128>::qo_height, fwd_attend_ker_tile_dims<128>::tile_width>>;
        using o_tile    =         st<DType, fwd_attend_ker_tile_dims<128>::qo_height, fwd_attend_ker_tile_dims<128>::tile_width>;

        using q_global = gl<QKDType,  -1, -1, -1, 128, q_tile>;
        using k_global = gl<QKDType,  -1, -1, -1, 128, k_tile>;
        using v_global = gl<DType,  -1, -1, -1, 128, v_tile>;
        // using l_global = gl<float, -1, -1, 1, -1, l_col_vec>;
        using o_global = gl<DType,  -1, -1, -1, 128, o_tile>;

        using globals      = fwd_globals<128>;

        q_global qg_arg{d_q, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};
        k_global kg_arg{d_k, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        v_global vg_arg{d_v, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        // l_global lg_arg{d_l, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), nullptr,   static_cast<unsigned int>(l_vec_stride_h)};
        o_global og_arg{d_o, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};

#if TK_ATTN_IS_FP8
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, d_scale_q, d_scale_k, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#else
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#endif

        auto mem_size = kittens::MAX_SHARED_MEMORY;
        // auto threads  = NUM_WORKERS * kittens::WARP_THREADS;

        constexpr int block_size_m = CONSUMER_WARPGROUPS*fwd_attend_ker_tile_dims<128>::qo_height;
        int num_m_blocks = (seq_len_q + block_size_m - 1) / block_size_m;
        dim3 grid(num_m_blocks, qo_heads, batch);

        if (is_causal) {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<128, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<128, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
        else {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<128, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<128, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
    }

    if (head_dim == 256) {
        constexpr int CONSUMER_WARPGROUPS = (2);
        constexpr int PRODUCER_WARPGROUPS = (1);
        constexpr int NUM_WARPGROUPS      = (CONSUMER_WARPGROUPS+PRODUCER_WARPGROUPS);
        constexpr int NUM_WORKERS         = (NUM_WARPGROUPS*kittens::WARPGROUP_WARPS);

        using q_tile    =         st<QKDType, fwd_attend_ker_tile_dims<256>::qo_height, fwd_attend_ker_tile_dims<256>::tile_width>;
        using k_tile    =         st<QKDType, fwd_attend_ker_tile_dims<256>::kv_height, fwd_attend_ker_tile_dims<256>::tile_width>;
        using v_tile    =         st<DType, fwd_attend_ker_tile_dims<256>::kv_height, fwd_attend_ker_tile_dims<256>::tile_width>;
        // using l_col_vec = col_vec<st_fl<fwd_attend_ker_tile_dims<256>::qo_height, fwd_attend_ker_tile_dims<256>::tile_width>>;
        using o_tile    =         st<DType, fwd_attend_ker_tile_dims<256>::qo_height, fwd_attend_ker_tile_dims<256>::tile_width>;

        using q_global = gl<QKDType,  -1, -1, -1, 256, q_tile>;
        using k_global = gl<QKDType,  -1, -1, -1, 256, k_tile>;
        using v_global = gl<DType,  -1, -1, -1, 256, v_tile>;
        // using l_global = gl<float, -1, -1, 1, -1, l_col_vec>;
        using o_global = gl<DType,  -1, -1, -1, 256, o_tile>;

        using globals      = fwd_globals<256>;

        q_global qg_arg{d_q, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};
        k_global kg_arg{d_k, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        v_global vg_arg{d_v, static_cast<unsigned int>(batch), static_cast<unsigned int>(kv_heads), static_cast<unsigned int>(seq_len_kv), nullptr};
        // l_global lg_arg{d_l, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), nullptr,   static_cast<unsigned int>(l_vec_stride_h)};
        o_global og_arg{d_o, static_cast<unsigned int>(batch), static_cast<unsigned int>(qo_heads), static_cast<unsigned int>(seq_len_q), nullptr};

#if TK_ATTN_IS_FP8
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, d_scale_q, d_scale_k, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#else
        globals g{qg_arg, kg_arg, vg_arg/* , lg_arg */, og_arg, static_cast<int>(seq_len_kv), static_cast<int>(hr)};
#endif

        auto mem_size = kittens::MAX_SHARED_MEMORY;
        // auto threads  = NUM_WORKERS * kittens::WARP_THREADS;

        constexpr int block_size_m = CONSUMER_WARPGROUPS*fwd_attend_ker_tile_dims<256>::qo_height;
        int num_m_blocks = (seq_len_q + block_size_m - 1) / block_size_m;
        dim3 grid(num_m_blocks, qo_heads, batch);

        if (is_causal) {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<256, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<256, true, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
        else {
            CHECK_CUDA_ERROR(cudaFuncSetAttribute(
                fwd_attend_ker<256, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS>,
                cudaFuncAttributeMaxDynamicSharedMemorySize,
                mem_size
            ));

            fwd_attend_ker<256, false, CONSUMER_WARPGROUPS, PRODUCER_WARPGROUPS><<<grid, (32*NUM_WORKERS), mem_size, stream>>>(g);
        }
    }

    C10_CUDA_KERNEL_LAUNCH_CHECK();

    return {o/* , l_vec */};
}
"""


@functools.cache
def load_tk_attention_module(dtype, is_fp8=False):
    extra_cuda_cflags = [
        "-std=c++20",
        # "-U__CUDA_NO_HALF_OPERATORS__",
        # "-U__CUDA_NO_HALF_CONVERSIONS__",
        # "-U__CUDA_NO_HALF2_OPERATORS__",
        # "-U__CUDA_NO_HALF2_CONVERSIONS__",
        # "-U__CUDA_NO_BFLOAT16_OPERATORS__",
        # "-U__CUDA_NO_BFLOAT16_CONVERSIONS__",
        # "-U__CUDA_NO_BFLOAT162_OPERATORS__",
        # "-U__CUDA_NO_BFLOAT162_CONVERSIONS__",
        "--expt-extended-lambda",
        "--expt-relaxed-constexpr",
        "--use_fast_math",
        # "--ptxas-options=-v",  # printing out number of registers
        # "--ptxas-options=--verbose,--register-usage-level=10,--warn-on-local-memory-usage",  # printing out number of registers
        "-lineinfo",
        "-O3",
        "-Xcudafe --diag_suppress=2361",
        "--threads=4",
        "-DNDEBUG",
        "-DKITTENS_HOPPER",
    ]
    if dtype == torch.float16:
        extra_cuda_cflags.append("-DTK_ATTN_DTYPE_FP16")
    elif dtype == torch.bfloat16:
        extra_cuda_cflags.append("-DTK_ATTN_DTYPE_BF16")
    else:
        raise ValueError(f"Unsupported dtype: {dtype}")

    if is_fp8:
        extra_cuda_cflags.append("-DTK_ATTN_IS_FP8")

    old_torch_cuda_arch_list = os.getenv("TORCH_CUDA_ARCH_LIST")
    os.environ["TORCH_CUDA_ARCH_LIST"] = "9.0a"
    try:
        module = load_inline(
            name=f"quantum_attn_tk_attention_dtype_{str(dtype).replace('torch.', '')}_is_fp8_{is_fp8}",
            cpp_sources=[
                "std::vector<torch::Tensor> attention_forward(const torch::Tensor &q, const torch::Tensor &k, const torch::Tensor &v, const torch::Tensor &scale_q, const torch::Tensor &scale_k, bool causal);"
                if is_fp8
                else "std::vector<torch::Tensor> attention_forward(const torch::Tensor &q, const torch::Tensor &k, const torch::Tensor &v, bool causal);"
            ],
            cuda_sources=[TK_ATTENTION_SOURCE],
            extra_cflags=["-std=c++20", "-O3", "-DNDEBUG"],
            extra_cuda_cflags=extra_cuda_cflags,
            extra_ldflags=["-lcuda", "-lcudart"],
            extra_include_paths=[utils.get_tk_include_dir()],
            functions=["attention_forward"],
            verbose=True,
        )
    finally:
        if old_torch_cuda_arch_list is None:
            os.environ.pop("TORCH_CUDA_ARCH_LIST")
        else:
            os.environ["TORCH_CUDA_ARCH_LIST"] = old_torch_cuda_arch_list
    return module
