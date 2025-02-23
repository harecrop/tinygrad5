import os
os.environ["NVIDIA_TF32_OVERRIDE"] = "0"
import unittest
import torch
torch.set_num_threads(1)
import time
import numpy as np
np.set_printoptions(linewidth=160)
from functools import partial
from tinygrad.ops import GlobalCounters
from tinygrad.tensor import Tensor
from tinygrad.nn import Conv2d
from tinygrad.helpers import colored
try:
  from tinygrad.llops.ops_gpu import CL
except ImportError:
  CL = None

IN_CHANS = [int(x) for x in os.getenv("IN_CHANS", "4,16,64").split(",")]

torch_device = torch.device('mps' if int(os.getenv("MPS", "0")) else ('cuda' if int(os.getenv("CUDA", "0")) else 'cpu'))

def colorize_float(x):
  ret = f"{x:7.2f}x"
  if x < 0.75:
    return colored(ret, 'green')
  elif x > 1.5:
    return colored(ret, 'red')
  else:
    return colored(ret, 'yellow')

save_ops, save_mem = 0, 0
CNT = 8
def helper_test_speed(f1, *args):
  global save_ops, save_mem
  ets = []
  ret = None
  for _ in range(CNT):
    del ret
    GlobalCounters.global_ops = 0
    GlobalCounters.global_mem = 0
    st = time.monotonic()
    ret = f1(*args)
    if CL is not None and ret.device in ["GPU"]:
      CL.cl_queue.finish()
    if torch_device != "cpu":
      # TODO: better way to sync?
      torch.zeros(1, device=torch_device).cpu()
    et = (time.monotonic() - st) * 1000
    ets.append(et)
    if GlobalCounters.global_ops:
      save_ops, save_mem = GlobalCounters.global_ops, GlobalCounters.global_mem
  return ret.cpu().numpy(), np.min(ets)

def helper_test_generic_square(name, N, f1, f2):
  torch.manual_seed(0)
  torch_a = (torch.rand(N, N) - 0.5).to(torch_device)
  torch_b = (torch.rand(N, N) - 0.5).to(torch_device)

  tiny_a = Tensor(torch_a.cpu().numpy())
  tiny_b = Tensor(torch_b.cpu().numpy())

  helper_test_generic(f"{name:30s} {N:4d}x{N:4d}", partial(f1, torch_a, torch_b), partial(f2, tiny_a, tiny_b))

prefix = None
def helper_test_generic(name, f1, f2):
  global prefix
  with torch.no_grad():
    val_torch, et_torch = helper_test_speed(f1)
  val_tinygrad, et_tinygrad = helper_test_speed(lambda: f2().realize())

  flops = save_ops*1e-6
  mem = save_mem*4*1e-6
  print(f"{prefix}{name:40s} {et_torch:7.2f} ms ({flops/et_torch:8.2f} GFLOPS {mem/et_torch:8.2f} GB/s) in torch, {et_tinygrad:7.2f} ms ({flops/et_tinygrad:8.2f} GFLOPS {mem/et_tinygrad:8.2f} GB/s) in tinygrad, {colorize_float(et_tinygrad/et_torch)} slower {flops:7.2f} MOPS {mem:7.2f} MB")
  prefix = " "
  np.testing.assert_allclose(val_tinygrad, val_torch, atol=1e-4, rtol=1e-3)

class TestSpeed(unittest.TestCase):
  def setUp(self):
    global prefix
    prefix = " " if prefix is None else ""
    return super().setUp()
  
  def test_sub(self):
    def f(a, b): return a-b
    helper_test_generic_square('sub', 4096, f, f)

  def test_pow(self):
    def f(a, b): return a.pow(b)
    helper_test_generic_square('pow', 2048, f, f)

  def test_sum(self):
    def f(a, b): return a.sum()
    helper_test_generic_square('sum', 4096, f, f)

  def test_partial_sum(self):
    R = 256
    def f(a, b): return a.reshape(int(4096//R), int(4096*R)).sum(axis=1)
    helper_test_generic_square('partial_sum', 4096, f, f)

  def test_array_packing(self):
    N = 2048
    def f(a, b): return a.reshape(N, N // 32, 32).permute(1,0,2).contiguous()
    helper_test_generic_square('array_packing', N, f, f)

  def test_permute(self):
    for N in [1024, 4096]:
      # this is a 64MB tensor, M1 L1 cache is 128kB
      # to fit easily in L1, rotations should be 128x128 chunks. 128x128 is also the AMX size
      def f(a, b): return a.permute(1,0).contiguous()
      helper_test_generic_square('permute', N, f, f)
    
  def test_double_permute(self):
    N = 64
    torch.manual_seed(0)
    torch_a = (torch.rand(N, N, N, N) - 0.5).to(torch_device)
    tiny_a = Tensor(torch_a.cpu().numpy())
    def f(a): return a.permute(1,0,3,2).contiguous()
    helper_test_generic(f"double_permute {tiny_a.shape}", partial(f, torch_a), partial(f, tiny_a))

  def test_neg(self):
    def f(a, b): return -a
    helper_test_generic_square('neg', 4096, f, f)

  def test_exp(self):
    def f(a, b): return a.exp()
    helper_test_generic_square('exp', 2048, f, f)

  def test_relu(self):
    def f(a, b): return a.relu()
    helper_test_generic_square('relu', 4096, f, f)

  def test_max(self):
    def f(a, b): return a.max()
    helper_test_generic_square('max', 4096, f, f)

  def test_mul_sum(self):
    def f(a, b): return (a*b).sum()
    helper_test_generic_square('mul_sum', 4096, f, f)

  def test_add(self):
    for N in [1024, 4096]:
      def f(a, b): return a + b
      helper_test_generic_square('add', N, f, f)

  def test_add_constant(self):
    def f(a, b): return a+2.0
    helper_test_generic_square('add_constant', 4096, f, f)

  def test_add_constant_zero(self):
    def f(a, b): return a+0.0
    helper_test_generic_square('add_constant_zero', 4096, f, f)

  def test_add_sq(self):
    def f(a, b): return a*a + b*b
    helper_test_generic_square('add_sq', 4096, f, f)

  def test_gemm(self):
    def f(a, b): return a @ b
    helper_test_generic_square('gemm', 512, f, f)

  def test_gemm_unrolled(self):
    N = 512
    def f1(a, b): return a@b.T
    def f2(a, b): return (a.reshape(N, 1, N).expand(N, N, N) * b.reshape(1, N, N).expand(N, N, N)).sum(axis=2)
    helper_test_generic_square('gemm_unrolled', N, f1, f2)
  
  def test_gemm_unrolled_permute_l(self):
    N = 512
    def f1(a, b): return a.T@b.T
    def f2(a, b): return (a.permute(1,0).reshape(N, 1, N).expand(N, N, N) * b.reshape(1, N, N).expand(N, N, N)).sum(axis=2)
    helper_test_generic_square('gemm_unrolled_permute_l', N, f1, f2)

  def test_gemm_unrolled_permute_r(self):
    N = 512
    def f1(a, b): return a@b
    def f2(a, b): return (a.reshape(N, 1, N).expand(N, N, N) * b.permute(1,0).reshape(1, N, N).expand(N, N, N)).sum(axis=2)
    helper_test_generic_square('gemm_unrolled_permute_r', N, f1, f2)

  def test_gemm_unrolled_permute_lr(self):
    N = 512
    def f1(a, b): return a.T@b
    def f2(a, b): return (a.permute(1,0).reshape(N, 1, N).expand(N, N, N) * b.permute(1,0).reshape(1, N, N).expand(N, N, N)).sum(axis=2)
    helper_test_generic_square('gemm_unrolled_permute_lr', N, f1, f2)

  def test_openpilot_conv2d(self):
    bs, in_chans, out_chans = 1,12,32
    torch.manual_seed(0)
    torch_dat = torch.rand(bs, 64, 128, 12).to(torch_device)
    torch_conv = torch.nn.Conv2d(in_chans, out_chans, 3, bias=None, padding=1).to(torch_device)

    tiny_dat = Tensor(torch_dat.cpu().numpy())
    tiny_conv = Conv2d(in_chans, out_chans, 3, bias=None, padding=1)
    tiny_conv.weight = Tensor(torch_conv.weight.detach().cpu().numpy())

    def f1(): return torch_conv(torch_dat.permute(0,3,1,2))
    def f2(): return tiny_conv(tiny_dat.permute(0,3,1,2)).realize()
    helper_test_generic(f"conv bs:{bs:3d} chans:{in_chans:3d} -> {out_chans:3d}", f1, f2)

  def test_conv2d(self):
    torch.manual_seed(0)
    for bs in [32]:
      for in_chans in IN_CHANS:
        for out_chans in [32]:
          img_size = 34
          torch_dat = torch.rand(bs, in_chans, img_size, img_size).to(torch_device)
          torch_conv = torch.nn.Conv2d(in_chans, out_chans, 3, bias=None).to(torch_device)

          tiny_dat = Tensor(torch_dat.cpu().numpy())
          tiny_conv = Conv2d(in_chans, out_chans, 3, bias=None)
          tiny_conv.weight = Tensor(torch_conv.weight.detach().cpu().numpy())

          def f1(): return torch_conv(torch_dat)
          def f2(): return tiny_conv(tiny_dat).realize()
          helper_test_generic(f"conv bs:{bs:3d} chans:{in_chans:3d} -> {out_chans:3d}", f1, f2)

if __name__ == '__main__':
  unittest.main()
