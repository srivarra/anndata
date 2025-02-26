from __future__ import annotations

import inspect
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Generic,
    Protocol,
    TypeVar,
    get_type_hints,
    runtime_checkable,
)
from warnings import warn

from anndata import AnnData

if TYPE_CHECKING:
    from collections.abc import Callable
import anndata as ad

# Based off of the extension framework in Polars
# https://github.com/pola-rs/polars/blob/main/py-polars/polars/api.py

__all__ = ["register_anndata_namespace", "ExtensionNamespace"]


def find_stacklevel() -> int:
    """
    Find the first place in the stack that is not inside AnnData.

    Taken from:
    https://github.com/pola-rs/polars/blob/main/py-polars/polars/_utils/various.py#L447
    """

    pkg_dir = str(Path(ad.__file__).parent)

    # https://stackoverflow.com/questions/17407119/python-inspect-stack-is-slow
    frame = inspect.currentframe()
    n = 0
    try:
        while frame:
            fname = inspect.getfile(frame)
            if fname.startswith(pkg_dir) or (
                (qualname := getattr(frame.f_code, "co_qualname", None))
                # ignore @singledispatch wrappers
                and qualname.startswith("singledispatch.")
            ):
                frame = frame.f_back
                n += 1
            else:
                break
    finally:
        # https://docs.python.org/3/library/inspect.html
        # > Though the cycle detector will catch these, destruction of the frames
        # > (and local variables) can be made deterministic by removing the cycle
        # > in a finally clause.
        del frame
    return n


NS = TypeVar("NS")


@runtime_checkable
class ExtensionNamespace(Protocol[NS]):
    """Protocol for extension namespaces.

    Enforces that the namespace initializer accepts a class with the proper `__init__` method.
    Protocol's can't enforce that the `__init__` accepts the correct types. See
    `_check_namespace_signature` for that. This is mainly useful for static type
    checking with mypy and IDEs.
    """

    def __init__(self, adata: AnnData) -> None:
        """
        Used to enforce the correct signature for extension namespaces.
        """
        ...


# Reserved namespaces include accessors built into AnnData (currently there are none)
# and all current attributes of AnnData
_reserved_namespaces: set[str] = set(AnnData._accessors) | set(dir(ad.AnnData))


class AccessorNameSpace(Generic[NS]):
    """Establish property-like namespace object for user-defined functionality."""

    def __init__(self, name: str, namespace: type[NS]) -> None:
        self._accessor = name
        self._ns = namespace

    def __get__(self, instance: NS | None, cls: type[NS]) -> NS | type[NS]:
        if instance is None:
            return self._ns

        ns_instance = self._ns(instance)  # type: ignore[call-arg]
        setattr(instance, self._accessor, ns_instance)
        return ns_instance


def _check_namespace_signature(ns_class: type) -> None:
    """Validate the signature of a namespace class for AnnData extensions.

    This function ensures that any class intended to be used as an extension namespace
    has a properly formatted `__init__` method such that:

    1. Accepts at least two parameters (self and adata)
    2. Has 'adata' as the name of the second parameter
    3. Has the second parameter properly type-annotated as 'AnnData' or any equivalent import alias

    The function performs runtime validation of these requirements before a namespace
    can be registered through the `register_anndata_namespace` decorator.

    Parameters
    ----------
    ns_class
        The namespace class to validate.

    Raises
    ------
    TypeError
        If the `__init__` method has fewer than 2 parameters (missing the AnnData parameter).
    AttributeError
        If the second parameter of `__init__` lacks a type annotation.
    TypeError
        If the second parameter of `__init__` is not named 'adata'.
    TypeError
        If the second parameter of `__init__` is not annotated as the 'AnnData' class.
    TypeError
        If both the name and type annotation of the second parameter are incorrect.

    """
    sig = inspect.signature(ns_class.__init__)
    params = list(sig.parameters.values())

    # Ensure there are at least two parameters (self and adata)
    if len(params) < 2:
        error_msg = "Namespace initializer must accept an AnnData instance as the second parameter."
        raise TypeError(error_msg)

    # Get the second parameter (expected to be 'adata')
    param = params[1]
    if param.annotation is inspect._empty:
        error_msg = "Namespace initializer's second parameter must be annotated as the 'AnnData' class."
        raise AttributeError(error_msg)

    name_ok = param.name == "adata"

    # Resolve the annotation using get_type_hints to handle forward references and aliases.
    try:
        type_hints = get_type_hints(ns_class.__init__)
        resolved_type = type_hints.get(param.name, param.annotation)
    except Exception:
        resolved_type = param.annotation

    type_ok = resolved_type is ad.AnnData

    match (name_ok, type_ok):
        case (True, True):
            return  # Signature is correct.
        case (False, True):
            msg = f"Namespace initializer's second parameter must be named 'adata', got '{param.name}'."
            raise TypeError(msg)
        case (True, False):
            type_repr = getattr(resolved_type, "__name__", str(resolved_type))
            msg = f"Namespace initializer's second parameter must be annotated as the 'AnnData' class, got '{type_repr}'."
            raise TypeError(msg)
        case _:
            type_repr = getattr(resolved_type, "__name__", str(resolved_type))
            msg = (
                f"Namespace initializer's second parameter must be named 'adata', got '{param.name}'. "
                f"And must be annotated as 'AnnData', got '{type_repr}'."
            )
            raise TypeError(msg)


def _create_namespace(name: str, cls: type[AnnData]) -> Callable[[type], type]:
    """Register custom namespace against the underlying AnnData class."""

    def namespace(ns_class: type) -> type:
        _check_namespace_signature(ns_class)  # Perform the runtime signature check
        if name in _reserved_namespaces:
            msg = f"cannot override reserved attribute {name!r}"
            raise AttributeError(msg)
        elif hasattr(cls, name):
            warn(
                f"Overriding existing custom namespace {name!r} (on {cls.__name__!r})",
                UserWarning,
                stacklevel=find_stacklevel(),
            )
        setattr(cls, name, AccessorNameSpace(name, ns_class))
        cls._accessors.add(name)
        return ns_class

    return namespace


def register_anndata_namespace(name: str) -> Callable[[type[NS]], type[NS]]:
    """Decorator for registering custom functionality with an :class:`~anndata.AnnData` object.

    This decorator allows you to extend AnnData objects with custom methods and properties
    organized under a namespace. The namespace becomes accessible as an attribute on AnnData
    instances, providing a clean way to you to add domain-specific functionality without modifying
    the AnnData class itself, or extending the class with additional methods as you see fit in your workflow.

    Parameters
    ----------
    name
        Name under which the accessor should be registered. This will be the attribute name
        used to access your namespace's functionality on AnnData objects (e.g., `adata.{name}`).
        Cannot conflict with existing AnnData attributes like `obs`, `var`, `X`, etc. The list of reserved
        attributes includes everything outputted by `dir(AnnData)`.

    Returns
    -------
        A decorator that registers the decorated class as a custom namespace.

    Notes
    -----
    Implementation requirements:

    1. The decorated class must have an `__init__` method that accepts exactly one parameter
       (besides `self`) named `adata` and annotated with type :class:`~anndata.AnnData`.
    2. The namespace will be initialized with the AnnData object on first access and then
       cached on the instance.
    3. If the namespace name conflicts with an existing namespace, a warning is issued.
    4. If the namespace name conflicts with a built-in AnnData attribute, an AttributeError is raised.

    Examples
    --------
    Simple transformation namespace:

    >>> import anndata as ad
    >>> import numpy as np
    >>>
    >>> @ad.register_anndata_namespace("transforms")
    ... class TransformX:
    ...     def __init__(self, adata: ad.AnnData):
    ...         self._adata = adata
    ...
    ...     def log1p(
    ...         self, layer: str = None, inplace: bool = False
    ...     ) -> ad.AnnData | None:
    ...         '''Log1p transform the data.'''
    ...         data = self._adata.layers[layer] if layer else self._adata.X
    ...         log1p_data = np.log1p(data)
    ...
    ...         if layer:
    ...             layer_name = f"{layer}_log1p" if not inplace else layer
    ...         else:
    ...             layer_name = "log1p"
    ...
    ...         self._adata.layers[layer_name] = log1p_data
    ...
    ...         if not inplace:
    ...             return self._adata
    >>>
    >>> # Create an AnnData object
    >>> rng = np.random.default_rng(42)
    >>> adata = ad.AnnData(X=rng.poisson(1, size=(100, 2000)))
    >>>
    >>> # Use the registered namespace
    >>> adata.transforms.log1p()  # Transforms X and returns the AnnData object
    AnnData object with n_obs × n_vars = 100 × 2000
        layers: 'log1p'

    """
    return _create_namespace(name, ad.AnnData)
