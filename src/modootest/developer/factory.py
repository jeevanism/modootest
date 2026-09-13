"""Declarative and imperative test data factory for Odoo models (OdooFactory)."""
from enum import Enum
import inspect
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union


class FactoryStrategy(Enum):
    """Internal execution strategy distinguishing build, new, and create."""
    BUILD = "build"
    NEW = "new"
    CREATE = "create"


class Sequence:
    """Auto-incrementing sequence generator for factory fields.

    Can be initialized with a formatting function, a format string template,
    or left empty to yield sequential integers.
    """

    def __init__(
        self,
        formatter: Optional[Union[Callable[[int], Any], str]] = None,
        start: int = 1,
        step: int = 1,
    ):
        self.formatter = formatter
        self.counter = start
        self.step = step

    def next(self) -> Any:
        """Increment and return the next formatted sequence value."""
        val = self.counter
        self.counter += self.step
        return self._format(val)

    def _format(self, n: int) -> Any:
        if self.formatter is None:
            return n
        if callable(self.formatter):
            return self.formatter(n)
        if isinstance(self.formatter, str):
            if "{seq}" in self.formatter:
                return self.formatter.format(seq=n)
            elif "{}" in self.formatter:
                return self.formatter.format(n)
            elif "%d" in self.formatter or "%s" in self.formatter:
                return self.formatter % n
            return f"{self.formatter}_{n}"
        return n

    def __call__(self, n: Optional[int] = None) -> Any:
        if n is not None:
            return self._format(n)
        return self.next()

    def reset(self, start: int = 1) -> None:
        """Reset sequence counter."""
        self.counter = start

    def __repr__(self) -> str:
        return f"<Sequence counter={self.counter} step={self.step}>"


def clone_container(val: Any) -> Any:
    """Recursively clones mutable containers (dict, list, set, tuple) while preserving
    identity of sequences, callables, factories, ORM recordsets, cursors, and environments.
    """
    if isinstance(val, (str, bytes, int, float, bool, type(None))):
        return val
    if isinstance(val, Sequence):
        return val
    if isinstance(val, type) and issubclass(val, RecordFactory):
        return val
    if callable(val):
        return val
    # Preserve ORM recordsets/models, environments, cursors, and MagicMocks
    if hasattr(val, "_name") and hasattr(val, "ids"):
        return val
    if hasattr(val, "cr") and hasattr(val, "registry"):
        return val
    if hasattr(val, "sql_log_count"):
        return val

    if isinstance(val, dict):
        return {k: clone_container(v) for k, v in val.items()}
    if isinstance(val, list):
        return [clone_container(v) for v in val]
    if isinstance(val, tuple):
        return tuple(clone_container(v) for v in val)
    if isinstance(val, set):
        return {clone_container(v) for v in val}

    return val


class RecordFactory:
    """Declarative base class for model factories.

    Example::

        class PartnerFactory(RecordFactory):
            _model = "res.partner"
            name = Sequence(lambda n: f"Customer {n}")
            is_company = True

        partner = odoo_factory.create(PartnerFactory, is_company=False)
    """

    _model: str = ""

    @classmethod
    def get_model(cls) -> str:
        """Retrieve model technical name from class or its ancestors."""
        for c in cls.mro():
            if issubclass(c, RecordFactory) and getattr(c, "_model", None):
                return c._model
        raise ValueError(f"Factory class {cls.__name__} must define '_model' attribute.")

    @classmethod
    def create(cls, env: Any, **overrides: Any) -> Any:
        """Create a record in the database using this factory and given environment."""
        return OdooFactory(env).create(cls, **overrides)

    @classmethod
    def create_batch(cls, env: Any, count: int, **overrides: Any) -> Any:
        """Create a batch of records in the database using this factory."""
        return OdooFactory(env).create_batch(cls, count, **overrides)

    @classmethod
    def new(cls, env: Any, **overrides: Any) -> Any:
        """Create an in-memory draft record (via env[model].new()) using this factory."""
        return OdooFactory(env).new(cls, **overrides)

    @classmethod
    def build(cls, env: Optional[Any] = None, **overrides: Any) -> Dict[str, Any]:
        """Build dictionary of evaluated field values without saving to database."""
        return OdooFactory(env).build(cls, **overrides)


class OdooFactory:
    """Test data factory for Odoo models, operating within the active transaction."""

    def __init__(self, env: Optional[Any] = None):
        self.env = env
        self._blueprints: Dict[str, Dict[str, Any]] = {}
        self._sequences: Dict[str, int] = {}

    def bind(self, env: Any) -> "OdooFactory":
        """Return a new OdooFactory bound to the given environment, sharing blueprints."""
        bound = OdooFactory(env)
        bound._blueprints = dict(self._blueprints)
        bound._sequences = dict(self._sequences)
        return bound

    def register(self, model_name: str, **defaults: Any) -> None:
        """Register default field values / blueprint for a model.

        :param model_name: Odoo model technical name (e.g. 'res.partner').
        :param defaults: Default field values, Callables, or Sequences.
        """
        self._blueprints[model_name] = clone_container(dict(defaults))

    def sequence(self, name: str = "default", start: int = 1, step: int = 1) -> int:
        """Generate the next integer in a named sequence."""
        if name not in self._sequences:
            self._sequences[name] = start
        val = self._sequences[name]
        self._sequences[name] += step
        return val

    def reset_sequences(self) -> None:
        """Reset all registered named sequences."""
        self._sequences.clear()

    def _resolve_model(self, model_or_factory: Union[str, Type[RecordFactory]]) -> str:
        if isinstance(model_or_factory, str):
            return model_or_factory
        if isinstance(model_or_factory, type) and issubclass(model_or_factory, RecordFactory):
            return model_or_factory.get_model()
        raise TypeError(
            f"Expected Odoo model name (str) or RecordFactory subclass, got {type(model_or_factory).__name__}"
        )

    def _extract_defaults(
        self, model_or_factory: Union[str, Type[RecordFactory]]
    ) -> Dict[str, Any]:
        defaults: Dict[str, Any] = {}

        if isinstance(model_or_factory, type) and issubclass(model_or_factory, RecordFactory):
            model_name = model_or_factory.get_model()
            # 1. Registered blueprint defaults for the model precede class defaults
            if model_name in self._blueprints:
                defaults.update(clone_container(self._blueprints[model_name]))

            # 2. Pull class attributes through the MRO from least to most specific
            framework_members = set(dir(RecordFactory)) | {"_model"}
            for cls in reversed(model_or_factory.mro()):
                if not issubclass(cls, RecordFactory) or cls is RecordFactory:
                    continue
                for attr, raw_val in cls.__dict__.items():
                    if attr.startswith("_") or attr in framework_members:
                        continue
                    if isinstance(raw_val, (property, classmethod, staticmethod)):
                        continue
                    if inspect.isroutine(raw_val) and not isinstance(raw_val, Sequence):
                        if inspect.isfunction(raw_val):
                            try:
                                sig = inspect.signature(raw_val)
                                params = list(sig.parameters.keys())
                                if params and params[0] in ("self", "cls"):
                                    continue
                            except (ValueError, TypeError):
                                pass
                    defaults[attr] = clone_container(raw_val)
        elif isinstance(model_or_factory, str):
            if model_or_factory in self._blueprints:
                defaults.update(clone_container(self._blueprints[model_or_factory]))

        return defaults

    def _evaluate_value(
        self,
        val: Any,
        seq_num: int,
        strategy: FactoryStrategy,
    ) -> Any:
        if isinstance(val, Sequence):
            return clone_container(val())

        if isinstance(val, type) and issubclass(val, RecordFactory):
            if strategy == FactoryStrategy.BUILD:
                return self.build(val)
            elif strategy == FactoryStrategy.NEW:
                if self.env is None:
                    raise RuntimeError("Cannot instantiate Odoo draft record without an active environment.")
                return self.new(val)
            elif strategy == FactoryStrategy.CREATE:
                if self.env is None:
                    raise RuntimeError("Cannot create Odoo record without an active environment.")
                created = self.create(val)
                return getattr(created, "id", created)

        if callable(val):
            # Inspect signature separately from calling to preserve user exceptions
            num_params = 0
            try:
                sig = inspect.signature(val)
                num_params = len([
                    p for p in sig.parameters.values()
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ])
            except (ValueError, TypeError):
                num_params = 0

            if num_params >= 1:
                res = val(seq_num)
            else:
                res = val()
            return clone_container(res)

        return clone_container(val)

    def _build_values(
        self,
        model_or_factory: Union[str, Type[RecordFactory]],
        strategy: FactoryStrategy,
        **overrides: Any,
    ) -> Dict[str, Any]:
        model_name = self._resolve_model(model_or_factory)
        fields_spec = self._extract_defaults(model_or_factory)

        # Overrides replace defaults before evaluation so overridden defaults
        # (e.g. nested factories or sequences) are never evaluated
        for key, val in overrides.items():
            fields_spec[key] = val

        seq_num = self.sequence(model_name)
        result: Dict[str, Any] = {}
        for key, raw_val in fields_spec.items():
            result[key] = self._evaluate_value(raw_val, seq_num, strategy)
        return result

    def build(
        self,
        model_or_factory: Union[str, Type[RecordFactory]],
        **overrides: Any,
    ) -> Dict[str, Any]:
        """Build and return a dictionary of evaluated field values without persisting.

        Nested factories will also be built as nested value dictionaries without any
        ORM create, new, write, or unlink calls.

        :param model_or_factory: Odoo model name or RecordFactory subclass.
        :param overrides: Specific field overrides for this instance.
        :return: Dict of evaluated field values.
        """
        return self._build_values(model_or_factory, FactoryStrategy.BUILD, **overrides)

    def new(
        self,
        model_or_factory: Union[str, Type[RecordFactory]],
        **overrides: Any,
    ) -> Any:
        """Create an in-memory draft record (via env[model].new()) without writing to DB.

        Nested factories will also be created as in-memory draft records via new().

        :param model_or_factory: Odoo model name or RecordFactory subclass.
        :param overrides: Field overrides.
        :return: In-memory Odoo draft recordset.
        """
        if self.env is None:
            raise RuntimeError("Cannot instantiate Odoo record without an active environment.")
        model_name = self._resolve_model(model_or_factory)
        values = self._build_values(model_or_factory, FactoryStrategy.NEW, **overrides)
        return self.env[model_name].new(values)

    def create(
        self,
        model_or_factory: Union[str, Type[RecordFactory]],
        **overrides: Any,
    ) -> Any:
        """Create and persist a record in the database within the active transaction.

        Nested factories will also be created and persisted in the database.

        :param model_or_factory: Odoo model name or RecordFactory subclass.
        :param overrides: Field overrides.
        :return: Created Odoo recordset.
        """
        if self.env is None:
            raise RuntimeError("Cannot create Odoo record without an active environment.")
        model_name = self._resolve_model(model_or_factory)
        values = self._build_values(model_or_factory, FactoryStrategy.CREATE, **overrides)
        return self.env[model_name].create(values)

    def create_batch(
        self,
        model_or_factory: Union[str, Type[RecordFactory]],
        count: int,
        **overrides: Any,
    ) -> Any:
        """Create and persist multiple records in a single batch operation.

        :param model_or_factory: Odoo model name or RecordFactory subclass.
        :param count: Number of records to create.
        :param overrides: Field overrides (callables/sequences are evaluated per record).
        :return: Created Odoo recordset containing all batch items.
        """
        if not isinstance(count, int) or count < 0:
            raise ValueError(f"count must be a non-negative integer, got {count!r}")
        if self.env is None:
            raise RuntimeError("Cannot create Odoo records without an active environment.")

        model_name = self._resolve_model(model_or_factory)
        if count == 0:
            return self.env[model_name].browse()

        vals_list = []
        for _ in range(count):
            row_overrides = {k: clone_container(v) for k, v in overrides.items()}
            vals_list.append(
                self._build_values(model_or_factory, FactoryStrategy.CREATE, **row_overrides)
            )

        return self.env[model_name].create(vals_list)
