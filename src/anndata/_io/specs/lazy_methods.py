from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import h5py
import numpy as np
from scipy import sparse

import anndata as ad
from anndata._core.file_backing import filename, get_elem_name
from anndata.compat import DaskArray, H5Array, H5Group, ZarrArray, ZarrGroup

from ..._settings import settings
from .registry import _LAZY_REGISTRY, IOSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence
    from typing import Literal, ParamSpec, TypeVar

    from anndata.experimental.backed._xarray import Dataset2D

    from ..._core.sparse_dataset import CSCDataset, CSRDataset
    from ..._types import ArrayStorageType, StorageType
    from .registry import DaskReader, LazyReader

    BlockInfo = Mapping[
        Literal[None],
        dict[str, Sequence[tuple[int, int]]],
    ]

    P = ParamSpec("P")
    R = TypeVar("R")


@contextmanager
def maybe_open_h5(
    path_or_group: Path | ZarrGroup, elem_name: str
) -> Generator[StorageType, None, None]:
    if not isinstance(path_or_group, Path):
        yield path_or_group
        return
    file = h5py.File(path_or_group, "r")
    try:
        yield file[elem_name]
    finally:
        file.close()


_DEFAULT_STRIDE = 1000


def compute_chunk_layout_for_axis_shape(
    chunk_axis_shape: int, full_axis_shape: int
) -> tuple[int, ...]:
    n_strides, rest = np.divmod(full_axis_shape, chunk_axis_shape)
    chunk = (chunk_axis_shape,) * n_strides
    if rest > 0:
        chunk += (rest,)
    return chunk


def make_dask_chunk(
    path_or_group: Path | ZarrGroup,
    elem_name: str,
    block_info: BlockInfo | None = None,
    *,
    wrap: Callable[[ArrayStorageType], ArrayStorageType]
    | Callable[[H5Group | ZarrGroup], CSRDataset | CSCDataset] = lambda g: g,
):
    if block_info is None:
        msg = "Block info is required"
        raise ValueError(msg)
    # We need to open the file in each task since `dask` cannot share h5py objects when using `dask.distributed`
    # https://github.com/scverse/anndata/issues/1105
    with maybe_open_h5(path_or_group, elem_name) as f:
        mtx = wrap(f)
        idx = tuple(
            slice(start, stop) for start, stop in block_info[None]["array-location"]
        )
        chunk = mtx[idx]
    return chunk


@_LAZY_REGISTRY.register_read(H5Group, IOSpec("csc_matrix", "0.1.0"))
@_LAZY_REGISTRY.register_read(H5Group, IOSpec("csr_matrix", "0.1.0"))
@_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("csc_matrix", "0.1.0"))
@_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("csr_matrix", "0.1.0"))
def read_sparse_as_dask(
    elem: H5Group | ZarrGroup,
    *,
    _reader: DaskReader,
    chunks: tuple[int, ...] | None = None,  # only tuple[int, int] is supported here
) -> DaskArray:
    import dask.array as da

    path_or_group = Path(filename(elem)) if isinstance(elem, H5Group) else elem
    elem_name = get_elem_name(elem)
    shape: tuple[int, int] = tuple(elem.attrs["shape"])
    dtype = elem["data"].dtype
    is_csc: bool = elem.attrs["encoding-type"] == "csc_matrix"

    stride: int = _DEFAULT_STRIDE
    major_dim, minor_dim = (1, 0) if is_csc else (0, 1)
    if chunks is not None:
        if len(chunks) != 2:
            raise ValueError("`chunks` must be a tuple of two integers")
        if chunks[minor_dim] != shape[minor_dim]:
            raise ValueError(
                "Only the major axis can be chunked. "
                f"Try setting chunks to {((-1, _DEFAULT_STRIDE) if is_csc else (_DEFAULT_STRIDE, -1))}"
            )
        stride = chunks[major_dim]

    shape_minor, shape_major = shape if is_csc else shape[::-1]
    chunks_major = compute_chunk_layout_for_axis_shape(stride, shape_major)
    chunks_minor = (shape_minor,)
    chunk_layout = (
        (chunks_minor, chunks_major) if is_csc else (chunks_major, chunks_minor)
    )
    memory_format = sparse.csc_matrix if is_csc else sparse.csr_matrix
    make_chunk = partial(
        make_dask_chunk, path_or_group, elem_name, wrap=ad.experimental.sparse_dataset
    )
    da_mtx = da.map_blocks(
        make_chunk,
        dtype=dtype,
        chunks=chunk_layout,
        meta=memory_format((0, 0), dtype=dtype),
    )
    return da_mtx


@_LAZY_REGISTRY.register_read(H5Array, IOSpec("string-array", "0.2.0"))
def read_h5_string_array(
    elem: H5Array,
    *,
    _reader: LazyReader,
    chunks: tuple[int, int] | None = None,
):
    import dask.array as da

    from anndata._io.h5ad import read_dataset

    return da.from_array(
        read_dataset(elem),
        chunks=chunks if chunks is not None else (_DEFAULT_STRIDE,) * len(elem.shape),
    )


@_LAZY_REGISTRY.register_read(H5Array, IOSpec("array", "0.2.0"))
def read_h5_array(
    elem: H5Array, *, _reader: LazyReader, chunks: tuple[int, ...] | None = None
) -> DaskArray:
    import dask.array as da

    path = Path(elem.file.filename)
    elem_name: str = elem.name
    shape = tuple(elem.shape)
    dtype = elem.dtype
    chunks: tuple[int, ...] = (
        chunks if chunks is not None else (_DEFAULT_STRIDE,) * len(shape)
    )

    chunk_layout = tuple(
        compute_chunk_layout_for_axis_shape(chunks[i], shape[i])
        for i in range(len(shape))
    )

    make_chunk = partial(make_dask_chunk, path, elem_name)
    return da.map_blocks(make_chunk, dtype=dtype, chunks=chunk_layout)


@_LAZY_REGISTRY.register_read(ZarrArray, IOSpec("string-array", "0.2.0"))
@_LAZY_REGISTRY.register_read(ZarrArray, IOSpec("array", "0.2.0"))
def read_zarr_array(
    elem: ZarrArray, *, _reader: LazyReader, chunks: tuple[int, ...] | None = None
) -> DaskArray:
    chunks: tuple[int, ...] = chunks if chunks is not None else elem.chunks
    import dask.array as da

    return da.from_zarr(elem, chunks=chunks)


@_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("dataframe", "0.2.0"))
@_LAZY_REGISTRY.register_read(H5Group, IOSpec("dataframe", "0.2.0"))
def read_dataframe(
    elem: H5Group | ZarrGroup,
    *,
    _reader: LazyReader,
    chunks: tuple[int, ...] | None = None,
) -> Dataset2D:
    from anndata.experimental.backed._compat import xr
    from anndata.experimental.backed._lazy_arrays import CategoricalArray, MaskedArray
    from anndata.experimental.backed._xarray import Dataset2D

    iter_object = [(k, elem[k]) for k in elem.attrs["column-order"]] + [
        (elem.attrs["_index"], elem[elem.attrs["_index"]])
    ]
    d = {k: _reader.read_elem(v) for k, v in iter_object}
    d_with_xr = {}
    elem_name = get_elem_name(elem)
    index_label = f'{elem_name.replace("/", "")}_names'
    index = d[elem.attrs["_index"]]  # no sense in reading this in multiple times
    for k in d:
        v = d[k]
        if isinstance(v, DaskArray) and k != elem.attrs["_index"]:
            d_with_xr[k] = xr.DataArray(v, coords=[index], dims=[index_label], name=k)
        elif (
            isinstance(v, CategoricalArray) or isinstance(v, MaskedArray)
        ) and k != elem.attrs["_index"]:
            variable = xr.Variable(
                data=xr.core.indexing.LazilyIndexedArray(v), dims=[index_label]
            )
            d_with_xr[k] = xr.DataArray(
                variable,
                coords=[index],
                dims=[index_label],
                name=k,
            )
        elif k == elem.attrs["_index"]:
            d_with_xr[index_label] = xr.DataArray(
                v, coords=[v], dims=[index_label], name=index_label
            )
        else:
            d_with_xr[k] = v
    return Dataset2D(d_with_xr)


@_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("categorical", "0.2.0"))
@_LAZY_REGISTRY.register_read(H5Group, IOSpec("categorical", "0.2.0"))
def read_categorical(
    elem: H5Group | ZarrGroup,
    *,
    _reader: LazyReader,
    chunks: tuple[int, ...] | None = None,
):
    from anndata.experimental.backed._lazy_arrays import CategoricalArray

    return CategoricalArray(
        codes=elem["codes"],
        categories=elem["categories"],
        ordered=elem.attrs["ordered"],
        drop_unused_cats=settings.should_remove_unused_categories,
    )


def read_nullable(
    elem: H5Group | ZarrGroup,
    *,
    encoding_type: str,
    _reader: LazyReader,
    chunks: tuple[int, ...] | None = None,
):
    from anndata.experimental.backed._lazy_arrays import MaskedArray

    return MaskedArray(
        values=elem["values"],
        mask=elem["mask"] if "mask" in elem else None,
        dtype_str=encoding_type,
    )


_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("nullable-integer", "0.1.0"))(
    partial(read_nullable, encoding_type="nullable-integer")
)
_LAZY_REGISTRY.register_read(H5Group, IOSpec("nullable-integer", "0.1.0"))(
    partial(read_nullable, encoding_type="nullable-integer")
)
_LAZY_REGISTRY.register_read(ZarrGroup, IOSpec("nullable-boolean", "0.1.0"))(
    partial(read_nullable, encoding_type="nullable-boolean")
)
_LAZY_REGISTRY.register_read(H5Group, IOSpec("nullable-boolean", "0.1.0"))(
    partial(read_nullable, encoding_type="nullable-boolean")
)
