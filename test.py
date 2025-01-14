import os
os.environ['XLA_FLAGS'] = "--xla_dump_hlo_as_proto --xla_dump_hlo_as_text --xla_dump_hlo_as_html --xla_dump_to=custom_part_ln_dump"

from functools import partial

import numpy as np
import jax
from jax import random
from jax import core, vmap
import jax.numpy as jnp
from jax.sharding import Mesh
from jax.experimental.custom_partitioning import custom_partitioning
from jax.experimental.pjit import pjit
from jax.sharding import PartitionSpec, NamedSharding
P = PartitionSpec

from transformer_engine.jax import cpp_extensions
from collections.abc import Sequence

# 0: ln-dot-dot, 1: dot-dot-ln
TEST_CASE = int(os.environ.get("TEST_CASE", 0))
assert TEST_CASE in (0, 1)


# First, we'll set up the user-facing layernorm function. It's backed by the
# layernorm_fwd_p primitive, which is different from (and sits on top of) the
# primitive in transformer_engine for reasons we'll see below. We use custom_vjp
# to set up the analogous layernorm_bwd_p primitive as its backward pass rule.

@partial(jax.custom_vjp, nondiff_argnums=(3, 4))
def layernorm(x, gamma, beta, zero_centered_gamma, epsilon):
    partial_ln = partial(_layernorm, gamma=gamma, beta=beta, zero_centered_gamma=zero_centered_gamma, epsilon=epsilon)
    out = vmap(partial_ln, in_axes=(0,))(x)
    return out

def _layernorm(x, gamma, beta, zero_centered_gamma, epsilon):
  z, *_ = layernorm_fwd_p.bind(x, gamma, beta,
                               zero_centered_gamma=zero_centered_gamma, epsilon=epsilon)
  return z

def layernorm_fwd_rule(x, gamma, beta, zero_centered_gamma, epsilon):
  z, mu, rsigma = layernorm_fwd_p.bind(
      x, gamma, beta, zero_centered_gamma=zero_centered_gamma, epsilon=epsilon)
  return z, (x, mu, rsigma, gamma)

def layernorm_bwd_rule(zero_centered_gamma, epsilon, res, dz):
  x, mu, rsigma, gamma = res
  dx, dgamma, dbeta = layernorm_bwd_p.bind(
      dz, x, mu, rsigma, gamma,
      zero_centered_gamma=zero_centered_gamma, epsilon=epsilon)
  return dx, dgamma, dbeta

layernorm.defvjp(layernorm_fwd_rule, layernorm_bwd_rule)


# Next, we'll define the two primitives, starting with layernorm_fwd_p. We'll
# set up vmap and lowering rules. The lowering rule will have the custom
# partitioning information, and will itself call into the CustomCall-backed
# primitive defined in transformer_engine.
#
# To be closed under vmapping, we'll define layernorm_fwd_p to handle
# arbitrarily many batch dimensions rather than just one.


layernorm_fwd_p = core.Primitive('layernorm_fwd')
layernorm_fwd_p.multiple_results = True

@layernorm_fwd_p.def_abstract_eval
def _layernorm_fwd_abstract_eval(x_aval, gamma_aval, beta_aval, *,
                                 zero_centered_gamma, epsilon):
  # x_aval: [N1, N2, ..., Nk, H]  # NOTE: multiple leading batch axes!
  # gamma_aval: [H]
  # beta_aval: [H]
  del gamma_aval, beta_aval, epsilon
  out_aval = core.raise_to_shaped(x_aval)
  mu_aval = rsigma_aval = out_aval.update(shape=out_aval.shape[:-1])
  return out_aval, mu_aval, rsigma_aval

# While the layernorm_fwd_p in this file supports multiple batch dimensions, the
# primitive in transformer_engine (and the GPU kernel we ultimately CustomCall
# into) handles just one. So we'll need to do some reshapes to flatten batch
# dimensions. It's important to do the reshapes here, "inside" layernorm_fwd_p
# rather than outside it, so that the SPMD partitioner doesn't need to handle
# the reshapes (as they'll be inside our custom_partitioning rule for
# layernorm_fwd_p).

@layernorm_fwd_p.def_impl
def layernorm_fwd_impl(x, gamma, beta, zero_centered_gamma, epsilon):
  pre = x.shape[:-1]
  x = x.reshape(-1, x.shape[-1])
  normed, mu, rsigma = cpp_extensions.layernorm_fwd(
      x, gamma, beta, zero_centered_gamma=zero_centered_gamma, epsilon=epsilon)
  return normed.reshape(*pre, -1), mu.reshape(pre), rsigma.reshape(pre)


# The vmap (batching) rule for layernorm_fwd_p just needs to put batch
# dimensions at the front.

from jax._src.interpreters import batching

def layernorm_fwd_batcher(
    batched_args: Sequence[jax.Array],
    batch_dims: Sequence[int | None],
    *,
    zero_centered_gamma: bool,
    epsilon: float,
) -> tuple[Sequence[jax.Array], Sequence[int | None]]:
  x, gamma, beta = batched_args
  x_bdim, gamma_bdim, beta_bdim = batch_dims
  if not (gamma_bdim is beta_bdim is batching.not_mapped):
    raise NotImplementedError
  if x_bdim == x.ndim - 1:
    x = jnp.moveaxis(x, -1, -2)
    x_bdim = x.ndim - 2
  out_bdims = x_bdim, batching.not_mapped, batching.not_mapped
  return layernorm_fwd_p.bind(x, gamma, beta, zero_centered_gamma=zero_centered_gamma, epsilon=epsilon), out_bdims
batching.primitive_batchers[layernorm_fwd_p] = layernorm_fwd_batcher


# The lowering rule for layernorm_fwd_p will be based on applying mlir.lower_fun
# to a custom_partitioning-decorated helper function.
# TODO(parkers,mattjj): This layering is a bit tricky, make it more convenient?

from jax._src.interpreters import mlir
from jax.experimental.custom_partitioning import custom_partitioning

_layernorm_fwd_lower = custom_partitioning(layernorm_fwd_impl,
                                           static_argnums=(3, 4))

# NOTE: `mesh` argument was added in the recent JAX commit 74bcd65
def infer_sharding_from_operands(zero_centered_gamma, epsilon, mesh, arg_infos, result_infos):
  del epsilon, result_infos  # Unused.
  x_spec = get_padded_spec(arg_infos[0])
  out_sharding = NamedSharding(mesh, P(*x_spec[:-1]))
  return (out_sharding,) * 3

# NOTE: `mesh` argument and output was added in the recent JAX commit 74bcd65
def partition(zero_centered_gamma, epsilon, mesh, arg_infos, result_infos):
  x_spec = get_padded_spec(arg_infos[0])
  arg_shardings = (NamedSharding(mesh, P(*x_spec[:-1], None)),
                   ) + (NamedSharding(mesh, P()),) * 2
  out_shardings = (NamedSharding(mesh, P(*x_spec[:-1])),) * 3
  impl = partial(layernorm_fwd_impl, zero_centered_gamma=zero_centered_gamma,
                 epsilon=epsilon)
  return mesh, impl, out_shardings, arg_shardings

_layernorm_fwd_lower.def_partition(
    infer_sharding_from_operands=infer_sharding_from_operands,
    partition=partition)

mlir.register_lowering(layernorm_fwd_p,
                       mlir.lower_fun(_layernorm_fwd_lower, multiple_results=True))


# Next, we set up the layernorm_bwd_p primitive. For an MVP, it needs fewer
# rules: in particular, if we don't need vmap-of-grad or grad-of-grad, and
# instead only need grad-of-vmap, then all it needs is a lowering rule.

layernorm_bwd_p = core.Primitive('layernorm_bwd')
layernorm_bwd_p.multiple_results = True

@layernorm_bwd_p.def_abstract_eval
def layernorm_bwd_abstract_eval(dz_aval, _, __, ___, gamma_aval, *,
                                zero_centered_gamma, epsilon):
  dx_aval = core.raise_to_shaped(dz_aval)
  dgamma_aval = dbeta_aval = core.raise_to_shaped(gamma_aval)
  return dx_aval, dgamma_aval, dbeta_aval

@layernorm_bwd_p.def_impl
def layernorm_bwd_impl(dz, x, mu, rsigma, gamma, zero_centered_gamma, epsilon):
  pre = x.shape[:-1]
  dz = dz.reshape(-1, x.shape[-1])
  x = x.reshape(-1, x.shape[-1])
  mu = mu.reshape(-1)
  rsigma = rsigma.reshape(-1)
  dx, dgamma, dbeta = cpp_extensions.layernorm_bwd(
      dz, mu, rsigma, x, gamma, zero_centered_gamma=zero_centered_gamma,
      epsilon=epsilon)
  return dx.reshape(*pre, -1), dgamma, dbeta

#Note that the batching rule for the bwd is not needed in this example,
#since we are doing grad-of-vmap, i.e. vmap would be in the network definition
def layernorm_bwd_batcher(
    batched_args: Sequence[jax.Array],
    batch_dims: Sequence[int | None],
    *,
    zero_centered_gamma: bool,
    epsilon: float,
) -> tuple[Sequence[jax.Array], Sequence[int | None]]:
  dz, x, mu, rsigma, gamma = batched_args
  dz_bdim, x_bdim, mu_bdim, rsigma_bdim, gamma_bdim = batch_dims
  if not (rsigma_bdim is gamma_bdim is batching.not_mapped):
    raise NotImplementedError
  if x_bdim == x.ndim - 1:
    x = jnp.moveaxis(x, -1, -2)
    x_bdim = x.ndim - 2
  out_bdims = dz_bdim, x_bdim, batching.not_mapped
  return layernorm_bwd_p.bind(dz, x, mu, rsigma, gamma, zero_centered_gamma=zero_centered_gamma, epsilon=epsilon), out_bdims
batching.primitive_batchers[layernorm_bwd_p] = layernorm_bwd_batcher

_layernorm_bwd_lower = custom_partitioning(layernorm_bwd_impl, static_argnums=(5, 6))
def infer_sharding_from_operands(
    zero_centered_gamma, epsilon, mesh, arg_infos, result_infos):
  x_spec = get_padded_spec(arg_infos[0])
  dx_sharding = NamedSharding(mesh, P(*x_spec[:-1], None))
  dgamma_sharding = dbeta_sharding = NamedSharding(mesh, P())
  return dx_sharding, dgamma_sharding, dbeta_sharding

def partition(zero_centered_gamma, epsilon, mesh, arg_infos, result_infos):
  x_spec = get_padded_spec(arg_infos[1])
  dx_sharding = NamedSharding(mesh, P(*x_spec[:-1], None))
  dgamma_sharding = dbeta_sharding = NamedSharding(mesh, P())
  out_shardings = dx_sharding, dgamma_sharding, dbeta_sharding
  arg_shardings = (NamedSharding(mesh, P(*x_spec[:-1])),) * 4
  arg_shardings = (*arg_shardings, NamedSharding(mesh, P()))

  def sharded_impl(dz, x, mu, rsigma, gamma):
    local_dx, local_dgamma, local_dbeta = \
        layernorm_bwd_impl(
            dz, x, mu, rsigma, gamma, zero_centered_gamma=zero_centered_gamma,
            epsilon=epsilon)
    global_dgamma = jax.lax.psum(local_dgamma, filter_none(x_spec[:-1]))
    global_dbeta  = jax.lax.psum( local_dbeta, filter_none(x_spec[:-1]))
    return local_dx, global_dgamma, global_dbeta

  return mesh, sharded_impl, out_shardings, arg_shardings

# NOTE: helper function no longer needed after the recent JAX commit 03575c4
def get_padded_spec(arg_info: jax.ShapeDtypeStruct) -> tuple:
  if arg_info.sharding is None:
    return (None,) * arg_info.ndim
  ndim, spec = arg_info.ndim, arg_info.sharding.spec
  assert len(spec) <= ndim
  return spec + (None,) * (ndim - len(spec))

def filter_none(xs: tuple) -> tuple:
  return tuple(x for x in xs if x is not None)

_layernorm_bwd_lower.def_partition(
    infer_sharding_from_operands=infer_sharding_from_operands,
    partition=partition
)


mlir.register_lowering(layernorm_bwd_p,
                       mlir.lower_fun(_layernorm_bwd_lower, multiple_results=True))

# Tests

# NOTE: wrapper which skips the layernorm_type argument; since it was unused
# it's not clear if it's needed
def _layernorm(x, gamma, beta, layernorm_type, zero_centered_gamma, epsilon):
  assert layernorm_type is None
  del layernorm_type
  return layernorm(x, gamma, beta, zero_centered_gamma, epsilon)

def func(x, gamma, beta, y1, y2):
    if TEST_CASE == 0:
        x = _layernorm(x, gamma, beta, None, False, 1e-6)
        x = jnp.dot(x, y1)
        out = jnp.dot(x, y2)
        return jnp.mean(out)
    else:
        x = jnp.dot(x, y1)
        x = jnp.dot(x, y2)
        out = _layernorm(x, gamma, beta, None, False, 1e-6)
        return jnp.mean(out)


x_ = random.normal(random.PRNGKey(1124), (2, 32, 128))
gamma_ = jnp.ones((128,))
beta_ = jnp.ones((128,))
y1_ = random.normal(random.PRNGKey(1126), (128, 128))
y2_ = random.normal(random.PRNGKey(1127), (128, 128))

graded_f = jax.value_and_grad(func, argnums=(0, 1, 2, 3, 4))
ref_l, ref_grads = graded_f(x_, gamma_, beta_, y1_, y2_)

devices = np.array(jax.local_devices())
if len(devices) == 2:
    devices = devices.reshape((2, 1))
elif len(devices) == 4:
    devices = devices.reshape((2, 2))
else:
    devices = devices.reshape((4, 2))

with Mesh(devices, ('x', 'y')) as mesh:
    x = jax.device_put(x_, NamedSharding(mesh, PartitionSpec('y', 'x', None)))
    gamma = jax.device_put(gamma_, NamedSharding(mesh, PartitionSpec(None)))
    beta = jax.device_put(beta_, NamedSharding(mesh, PartitionSpec(None)))
    y1 = jax.device_put(y1_, NamedSharding(mesh, PartitionSpec(None, 'y')))
    y2 = jax.device_put(y2_, NamedSharding(mesh, PartitionSpec('y', None)))

    pjitter = pjit(graded_f,
                   in_shardings=[PartitionSpec('y', 'x', None), PartitionSpec(None), PartitionSpec(None),
                                      PartitionSpec(None, 'y'), PartitionSpec('y', None)],
                   out_shardings=(None, (PartitionSpec('y', 'x', None), PartitionSpec(None), PartitionSpec(None),
                                              PartitionSpec(None, 'y'), PartitionSpec('y', None)))
              )

    test_l, test_grads = pjitter(x, gamma, beta, y1, y2)

print("TEST_CASE:", TEST_CASE)
print("loss match:", jnp.allclose(ref_l, test_l))
print("dgrad match:", jnp.allclose(ref_grads[0], test_grads[0], rtol=1e-04))
print("dgamma match:", jnp.allclose(ref_grads[1], test_grads[1]))
print("dbeta match:", jnp.allclose(ref_grads[2], test_grads[2], rtol=1e-03))
print("dy1 match:", jnp.allclose(ref_grads[3], test_grads[3]))
print("dy2 match:", jnp.allclose(ref_grads[4], test_grads[4]))