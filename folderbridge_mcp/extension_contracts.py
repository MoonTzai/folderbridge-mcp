from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


SUPPORTED_EXTENSION_SCHEMA_VERSIONS = (1, 2)
SUPPORTED_RUNTIME_ABI_VERSIONS = (1, 2)
EFFECT_SEMANTICS = frozenset({"read_only", "guarded_replay_safe", "external_effect", "unclassified_unsafe"})
EFFECT_LIFETIMES = frozenset({"foreground", "job_owned", "managed_service"})
RECOVERY_CORRELATIONS = frozenset({"none", "host_operation_id", "deterministic_fields"})
RECOVERY_CAPSULE_KINDS = frozenset(
    {"enum", "boolean", "integer", "workspace_path", "stable_identifier", "digest"}
)
MAX_RECOVERY_CAPSULE_FIELDS = 16


@dataclass(frozen=True)
class ResolvedEffectContract:
    effect_semantics: str
    lifetime: str
    trusted: bool


@dataclass(frozen=True)
class EffectContractSpec:
    direct: ResolvedEffectContract | None = None
    selector_param: str | None = None
    selector_cases: tuple[tuple[str, ResolvedEffectContract], ...] = ()

    def describe(self) -> dict[str, Any]:
        if self.direct is not None:
            return {
                "effect_semantics": self.direct.effect_semantics,
                "lifetime": self.direct.lifetime,
            }
        return {
            "selector": {
                "param": self.selector_param,
                "cases": {
                    key: {
                        "effect_semantics": value.effect_semantics,
                        "lifetime": value.lifetime,
                    }
                    for key, value in self.selector_cases
                },
            }
        }


@dataclass(frozen=True)
class RecoveryCapsuleField:
    param: str
    kind: str


@dataclass(frozen=True)
class RecoveryContractSpec:
    correlation: str | None = None
    capsule: tuple[RecoveryCapsuleField, ...] = ()
    recovery_mode_control: bool = False
    reconcile_action: str | None = None
    selector_param: str | None = None
    selector_cases: tuple[tuple[str, "RecoveryContractSpec"], ...] = ()

    @property
    def is_selector(self) -> bool:
        return self.selector_param is not None

    def describe(self) -> dict[str, Any]:
        if self.is_selector:
            return {
                "selector": {
                    "param": self.selector_param,
                    "cases": {key: value.describe() for key, value in self.selector_cases},
                }
            }
        result: dict[str, Any] = {
            "correlation": self.correlation,
            "recovery_mode_control": self.recovery_mode_control,
        }
        if self.capsule:
            result["capsule"] = [{"param": item.param, "kind": item.kind} for item in self.capsule]
        if self.reconcile_action is not None:
            result["reconcile_action"] = self.reconcile_action
        return result


def _top_level_enum(input_schema: dict[str, Any], param: Any, *, action_name: str, field: str) -> tuple[str, ...]:
    if not isinstance(param, str) or not param:
        raise ValueError(f"action {action_name} {field} selector param must be a non-empty string")
    properties = input_schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    property_schema = properties.get(param)
    if not isinstance(property_schema, dict) or property_schema.get("type") != "string":
        raise ValueError(f"action {action_name} {field} selector must reference a top-level string property")
    enum = property_schema.get("enum")
    if (
        not isinstance(enum, list)
        or not enum
        or not all(isinstance(item, str) and item for item in enum)
        or len(enum) != len(set(enum))
    ):
        raise ValueError(f"action {action_name} {field} selector property must declare a unique string enum")
    return tuple(enum)


def _parse_direct_effect(raw: Any, *, action_name: str, where: str) -> ResolvedEffectContract:
    if not isinstance(raw, dict) or set(raw) != {"effect_semantics", "lifetime"}:
        raise ValueError(f"action {action_name} {where} must declare exactly effect_semantics and lifetime")
    effect_semantics = raw.get("effect_semantics")
    lifetime = raw.get("lifetime")
    if effect_semantics not in EFFECT_SEMANTICS:
        raise ValueError(f"action {action_name} {where}.effect_semantics is unsupported")
    if lifetime not in EFFECT_LIFETIMES:
        raise ValueError(f"action {action_name} {where}.lifetime is unsupported")
    return ResolvedEffectContract(effect_semantics, lifetime, True)


def parse_effect_contract(
    raw: Any,
    *,
    schema_version: int,
    action_name: str,
    input_schema: dict[str, Any],
) -> EffectContractSpec | None:
    if schema_version == 1:
        if raw is not None:
            raise ValueError(f"action {action_name} effect_contract requires manifest schema v2")
        return None
    if raw is None:
        raise ValueError(f"action {action_name} schema v2 requires effect_contract")
    if not isinstance(raw, dict):
        raise ValueError(f"action {action_name} effect_contract must be an object")
    if "selector" not in raw:
        return EffectContractSpec(direct=_parse_direct_effect(raw, action_name=action_name, where="effect_contract"))
    if set(raw) != {"selector"}:
        raise ValueError(f"action {action_name} selector effect_contract may contain only selector")
    selector = raw.get("selector")
    if not isinstance(selector, dict) or set(selector) != {"param", "cases"}:
        raise ValueError(f"action {action_name} effect_contract selector must contain exactly param and cases")
    enum = _top_level_enum(input_schema, selector.get("param"), action_name=action_name, field="effect_contract")
    raw_cases = selector.get("cases")
    if not isinstance(raw_cases, dict) or set(raw_cases) != set(enum):
        raise ValueError(f"action {action_name} effect_contract selector cases must cover the exact validated enum")
    cases = tuple(
        (
            key,
            _parse_direct_effect(
                raw_cases[key],
                action_name=action_name,
                where=f"effect_contract.selector.cases.{key}",
            ),
        )
        for key in enum
    )
    return EffectContractSpec(selector_param=selector["param"], selector_cases=cases)


def resolve_effect_contract_spec(
    spec: EffectContractSpec | None,
    *,
    run_mode: str,
    params: dict[str, Any],
) -> ResolvedEffectContract:
    if spec is None:
        # ABI1/schema-v1 compatibility remains executable, but its workspace
        # read_only flag is deliberately not effect-safety evidence.
        lifetime = "job_owned" if run_mode == "job" else "foreground"
        return ResolvedEffectContract("unclassified_unsafe", lifetime, False)
    if spec.direct is not None:
        return spec.direct
    value = params.get(spec.selector_param or "")
    for key, contract in spec.selector_cases:
        if value == key and type(value) is type(key):
            return contract
    raise ValueError("effect_contract selector value was not validated before resolution")


def _validate_capsule_field(
    raw: Any,
    *,
    action_name: str,
    input_schema: dict[str, Any],
    index: int,
) -> RecoveryCapsuleField:
    if not isinstance(raw, dict) or set(raw) != {"param", "kind"}:
        raise ValueError(f"action {action_name} recovery capsule field {index} must contain exactly param and kind")
    param = raw.get("param")
    kind = raw.get("kind")
    if not isinstance(param, str) or not param:
        raise ValueError(f"action {action_name} recovery capsule field {index} param must be a non-empty string")
    if kind not in RECOVERY_CAPSULE_KINDS:
        raise ValueError(f"action {action_name} recovery capsule field {index} kind is unsupported")
    properties = input_schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    property_schema = properties.get(param)
    if not isinstance(property_schema, dict):
        raise ValueError(f"action {action_name} recovery capsule param {param!r} must reference a top-level property")
    property_type = property_schema.get("type")
    if kind == "boolean" and property_type != "boolean":
        raise ValueError(f"action {action_name} recovery capsule boolean param {param!r} must be boolean")
    if kind == "integer" and property_type != "integer":
        raise ValueError(f"action {action_name} recovery capsule integer param {param!r} must be integer")
    if kind in {"enum", "stable_identifier"}:
        enum = property_schema.get("enum")
        if not isinstance(enum, list) or not enum or not all(isinstance(item, (str, int, bool)) for item in enum):
            raise ValueError(f"action {action_name} recovery capsule {kind} param {param!r} requires a bounded enum")
    if kind == "workspace_path" and property_type != "string":
        raise ValueError(f"action {action_name} recovery capsule workspace_path param {param!r} must be string")
    if kind == "digest" and property_type not in {"string", "integer", "number", "boolean"}:
        raise ValueError(f"action {action_name} recovery capsule digest param {param!r} must be a scalar")
    return RecoveryCapsuleField(param, kind)


def _parse_direct_recovery(
    raw: Any,
    *,
    action_name: str,
    input_schema: dict[str, Any],
    where: str,
) -> RecoveryContractSpec:
    allowed = {"correlation", "capsule", "recovery_mode_control", "reconcile_action"}
    if not isinstance(raw, dict) or set(raw).difference(allowed):
        raise ValueError(f"action {action_name} {where} has unsupported recovery contract fields")
    correlation = raw.get("correlation")
    if correlation not in RECOVERY_CORRELATIONS:
        raise ValueError(f"action {action_name} {where}.correlation is unsupported")
    recovery_mode_control = raw.get("recovery_mode_control", False)
    if not isinstance(recovery_mode_control, bool):
        raise ValueError(f"action {action_name} {where}.recovery_mode_control must be boolean")
    reconcile_action = raw.get("reconcile_action")
    if reconcile_action is not None and (not isinstance(reconcile_action, str) or not reconcile_action):
        raise ValueError(f"action {action_name} {where}.reconcile_action must be a non-empty action name")

    raw_capsule = raw.get("capsule", [])
    if not isinstance(raw_capsule, list) or len(raw_capsule) > MAX_RECOVERY_CAPSULE_FIELDS:
        raise ValueError(
            f"action {action_name} {where}.capsule must be a list of <= {MAX_RECOVERY_CAPSULE_FIELDS} descriptors"
        )
    if correlation == "deterministic_fields":
        if not raw_capsule:
            raise ValueError(f"action {action_name} deterministic_fields recovery requires a bounded capsule")
    elif raw_capsule:
        raise ValueError(f"action {action_name} recovery capsule is allowed only for deterministic_fields correlation")

    capsule = tuple(
        _validate_capsule_field(
            item,
            action_name=action_name,
            input_schema=input_schema,
            index=index,
        )
        for index, item in enumerate(raw_capsule)
    )
    if len({item.param for item in capsule}) != len(capsule):
        raise ValueError(f"action {action_name} recovery capsule may not capture the same param twice")
    return RecoveryContractSpec(
        correlation=correlation,
        capsule=capsule,
        recovery_mode_control=recovery_mode_control,
        reconcile_action=reconcile_action,
    )


def parse_recovery_contract(
    raw: Any,
    *,
    schema_version: int,
    action_name: str,
    input_schema: dict[str, Any],
) -> RecoveryContractSpec | None:
    if schema_version == 1:
        if raw is not None:
            raise ValueError(f"action {action_name} recovery_contract requires manifest schema v2")
        return None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"action {action_name} recovery_contract must be an object")
    if "selector" not in raw:
        return _parse_direct_recovery(
            raw,
            action_name=action_name,
            input_schema=input_schema,
            where="recovery_contract",
        )
    if set(raw) != {"selector"}:
        raise ValueError(f"action {action_name} selector recovery_contract may contain only selector")
    selector = raw.get("selector")
    if not isinstance(selector, dict) or set(selector) != {"param", "cases"}:
        raise ValueError(f"action {action_name} recovery_contract selector must contain exactly param and cases")
    enum = _top_level_enum(input_schema, selector.get("param"), action_name=action_name, field="recovery_contract")
    raw_cases = selector.get("cases")
    if not isinstance(raw_cases, dict) or set(raw_cases) != set(enum):
        raise ValueError(f"action {action_name} recovery_contract selector cases must cover the exact validated enum")
    cases = tuple(
        (
            key,
            _parse_direct_recovery(
                raw_cases[key],
                action_name=action_name,
                input_schema=input_schema,
                where=f"recovery_contract.selector.cases.{key}",
            ),
        )
        for key in enum
    )
    return RecoveryContractSpec(selector_param=selector["param"], selector_cases=cases)


def iter_reconcile_actions(spec: RecoveryContractSpec | None) -> Iterable[str]:
    if spec is None:
        return ()
    if spec.is_selector:
        return tuple(
            action
            for _key, case in spec.selector_cases
            for action in iter_reconcile_actions(case)
        )
    return (spec.reconcile_action,) if spec.reconcile_action is not None else ()
