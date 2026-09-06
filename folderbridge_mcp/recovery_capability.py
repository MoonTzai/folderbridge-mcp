from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .extension_contracts import (
    RecoveryContractSpec,
    ResolvedEffectContract,
    resolve_effect_contract_spec,
)


@dataclass(frozen=True)
class TransportSettlementCapability:
    mode: str
    identity_namespace: str | None = None
    retry_horizon_seconds: float | None = None
    correlation_proven: bool = False
    retry_stable: bool = False
    non_reuse_proven: bool = False
    loss_reordering_proven: bool = False
    failure_signal_proven: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {
            "unproven",
            "structured_delivery_ack",
            "retry_identity",
            "bounded_failure_quarantine",
        }:
            raise ValueError("unsupported settlement capability mode")
        horizon = self.retry_horizon_seconds
        if horizon is not None and (
            isinstance(horizon, bool)
            or not isinstance(horizon, (int, float))
            or not math.isfinite(float(horizon))
            or float(horizon) <= 0
        ):
            raise ValueError("settlement horizon must be a positive finite number")
        if self.mode == "structured_delivery_ack":
            if not self.correlation_proven or not self.loss_reordering_proven:
                raise ValueError(
                    "structured delivery acknowledgement requires proven correlation and loss/reordering semantics"
                )
        elif self.mode == "retry_identity":
            if not isinstance(self.identity_namespace, str) or not self.identity_namespace:
                raise ValueError("retry identity requires a non-empty identity namespace")
            if horizon is None or not self.retry_stable or not self.non_reuse_proven:
                raise ValueError(
                    "retry identity requires a bounded horizon plus retry-stable and non-reuse proof"
                )
        elif self.mode == "bounded_failure_quarantine":
            if horizon is None or not self.failure_signal_proven:
                raise ValueError(
                    "bounded failure quarantine requires a proven failure signal and bounded horizon"
                )

    @property
    def proven_bounded(self) -> bool:
        return self.mode != "unproven"

    @classmethod
    def unproven(cls) -> "TransportSettlementCapability":
        return cls("unproven")

    @classmethod
    def structured_delivery_ack(
        cls,
        *,
        correlation_proven: bool,
        loss_reordering_proven: bool,
    ) -> "TransportSettlementCapability":
        return cls(
            "structured_delivery_ack",
            correlation_proven=correlation_proven,
            loss_reordering_proven=loss_reordering_proven,
        )

    @classmethod
    def retry_identity(
        cls,
        *,
        identity_namespace: str,
        retry_horizon_seconds: float,
        retry_stable: bool,
        non_reuse_proven: bool,
    ) -> "TransportSettlementCapability":
        return cls(
            "retry_identity",
            identity_namespace=identity_namespace,
            retry_horizon_seconds=float(retry_horizon_seconds),
            retry_stable=retry_stable,
            non_reuse_proven=non_reuse_proven,
        )

    @classmethod
    def bounded_failure_quarantine(
        cls,
        *,
        quarantine_horizon_seconds: float,
        failure_signal_proven: bool,
    ) -> "TransportSettlementCapability":
        return cls(
            "bounded_failure_quarantine",
            retry_horizon_seconds=float(quarantine_horizon_seconds),
            failure_signal_proven=failure_signal_proven,
        )


@dataclass(frozen=True)
class RecoveryAdmissionDecision:
    allowed: bool
    reason: str
    owner: str
    effect_semantics: str
    lifetime: str


def _resolve_recovery_contract(
    spec: RecoveryContractSpec | None,
    params: dict[str, Any],
) -> RecoveryContractSpec | None:
    if spec is None or not spec.is_selector:
        return spec
    value = params.get(spec.selector_param or "")
    for key, case in spec.selector_cases:
        if value == key and type(value) is type(key):
            return case
    return None


@dataclass(frozen=True)
class RecoveryAdmissionContext:
    transport_mode: str
    settlement: TransportSettlementCapability
    durable_effect_boundary_available: bool = False
    journal_capacity_available: bool = False
    pin_capacity_available: bool = False
    key_capacity_available: bool = False

    def __post_init__(self) -> None:
        if self.transport_mode not in {"direct_stdio", "persistent_tunnel"}:
            raise ValueError("transport_mode must be direct_stdio or persistent_tunnel")

    @classmethod
    def direct_stdio(cls) -> "RecoveryAdmissionContext":
        return cls(
            transport_mode="direct_stdio",
            settlement=TransportSettlementCapability.unproven(),
        )

    @classmethod
    def persistent_tunnel_unproven(cls) -> "RecoveryAdmissionContext":
        return cls(
            transport_mode="persistent_tunnel",
            settlement=TransportSettlementCapability.unproven(),
        )

    def assess_unclassified_owner(
        self,
        *,
        owner: str,
        lifetime: str,
    ) -> RecoveryAdmissionDecision:
        return self.assess_effect(
            owner=owner,
            effect=ResolvedEffectContract(
                effect_semantics="unclassified_unsafe",
                lifetime=lifetime,
                trusted=False,
            ),
            recovery_contract=None,
        )

    def assess_extension(
        self,
        *,
        owner: str,
        effect: ResolvedEffectContract,
        recovery_contract: RecoveryContractSpec | None,
        params: dict[str, Any],
    ) -> RecoveryAdmissionDecision:
        return self.assess_effect(
            owner=owner,
            effect=effect,
            recovery_contract=_resolve_recovery_contract(recovery_contract, params),
        )

    def assess_effect(
        self,
        *,
        owner: str,
        effect: ResolvedEffectContract,
        recovery_contract: RecoveryContractSpec | None,
    ) -> RecoveryAdmissionDecision:
        if self.transport_mode == "direct_stdio":
            return RecoveryAdmissionDecision(
                True,
                "direct_stdio_no_remote_settlement_contract",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if not effect.trusted:
            return RecoveryAdmissionDecision(
                False,
                "effect_contract_untrusted",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if effect.effect_semantics in {"read_only", "guarded_replay_safe"}:
            return RecoveryAdmissionDecision(
                True,
                "effect_replay_safe",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if effect.effect_semantics == "unclassified_unsafe":
            return RecoveryAdmissionDecision(
                False,
                "effect_semantics_unclassified",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if not self.durable_effect_boundary_available:
            return RecoveryAdmissionDecision(
                False,
                "durable_effect_boundary_unavailable",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if recovery_contract is None or recovery_contract.correlation in {None, "none"}:
            return RecoveryAdmissionDecision(
                False,
                "owner_recovery_contract_unavailable",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if not (
            self.journal_capacity_available
            and self.pin_capacity_available
            and self.key_capacity_available
        ):
            return RecoveryAdmissionDecision(
                False,
                "recovery_capacity_unavailable",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        if not self.settlement.proven_bounded:
            return RecoveryAdmissionDecision(
                False,
                "transport_settlement_unproven",
                owner,
                effect.effect_semantics,
                effect.lifetime,
            )

        return RecoveryAdmissionDecision(
            True,
            "bounded_settlement_proven",
            owner,
            effect.effect_semantics,
            effect.lifetime,
        )


def build_extension_settlement_matrix(registry: Any, context: RecoveryAdmissionContext) -> list[dict[str, Any]]:
    records, _errors = registry.scan()
    rows: list[dict[str, Any]] = []
    for extension_id in sorted(records):
        record = records[extension_id]
        for action_name in sorted(record.manifest.actions):
            action = record.manifest.actions[action_name]
            if action.effect_contract is None:
                effect = resolve_effect_contract_spec(
                    None,
                    run_mode=action.run_mode,
                    params={},
                )
                decision = context.assess_effect(
                    owner=f"extension:{extension_id}",
                    effect=effect,
                    recovery_contract=None,
                )
                recovery = None
            elif action.effect_contract.direct is not None:
                effect = action.effect_contract.direct
                recovery = (
                    action.recovery_contract
                    if action.recovery_contract is None or not action.recovery_contract.is_selector
                    else None
                )
                decision = context.assess_effect(
                    owner=f"extension:{extension_id}",
                    effect=effect,
                    recovery_contract=recovery,
                )
            else:
                # Matrix rows must not guess selector values. Emit one exact row
                # per declared effect case, with the matching recovery case when
                # the recovery selector uses the same discriminator.
                recovery_by_key: dict[str, RecoveryContractSpec] = {}
                if (
                    action.recovery_contract is not None
                    and action.recovery_contract.is_selector
                    and action.recovery_contract.selector_param == action.effect_contract.selector_param
                ):
                    recovery_by_key = dict(action.recovery_contract.selector_cases)
                for selector_value, effect in action.effect_contract.selector_cases:
                    recovery = recovery_by_key.get(selector_value)
                    decision = context.assess_effect(
                        owner=f"extension:{extension_id}",
                        effect=effect,
                        recovery_contract=recovery,
                    )
                    rows.append(
                        {
                            "owner": f"extension:{extension_id}",
                            "action": action_name,
                            "selector_param": action.effect_contract.selector_param,
                            "selector_value": selector_value,
                            "owner_contract_digest": record.sha256,
                            "manifest_schema_version": record.manifest.schema_version,
                            "runtime_abi": record.manifest.runtime_abi,
                            "effect_semantics": effect.effect_semantics,
                            "lifetime": effect.lifetime,
                            "effect_contract_trusted": effect.trusted,
                            "recovery_contract": recovery.describe() if recovery is not None else None,
                            "transport_settlement_mode": context.settlement.mode,
                            "persistent_tunnel_allowed": decision.allowed,
                            "blocking_reason": None if decision.allowed else decision.reason,
                        }
                    )
                continue

            rows.append(
                {
                    "owner": f"extension:{extension_id}",
                    "action": action_name,
                    "selector_param": None,
                    "selector_value": None,
                    "owner_contract_digest": record.sha256,
                    "manifest_schema_version": record.manifest.schema_version,
                    "runtime_abi": record.manifest.runtime_abi,
                    "effect_semantics": effect.effect_semantics,
                    "lifetime": effect.lifetime,
                    "effect_contract_trusted": effect.trusted,
                    "recovery_contract": recovery.describe() if recovery is not None else None,
                    "transport_settlement_mode": context.settlement.mode,
                    "persistent_tunnel_allowed": decision.allowed,
                    "blocking_reason": None if decision.allowed else decision.reason,
                }
            )
    return rows
