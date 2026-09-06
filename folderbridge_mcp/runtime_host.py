from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Iterable

from . import __version__
from .mcp import McpRequestScheduler, McpServer
from .operation_registry import (
    REGISTRY_COMPAT_SCHEMA_VERSIONS,
    REGISTRY_SCHEMA_VERSION,
    OperationRegistry,
)
from .recovery_capability import RecoveryAdmissionContext, TransportSettlementCapability
from .runtime_transport import (
    GenerationAdmission,
    PrivateMcpHttpServer,
    is_recovery_control_request,
)
from .tools import ToolRuntime
from .user_paths import user_config_root


_BOOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class RuntimeHostError(RuntimeError):
    pass


class RuntimeRecoveryBlocked(RuntimeHostError):
    pass


class RuntimeHost:
    """Phase-0 persistent RuntimeHost skeleton.

    This object deliberately does not change Launcher/Tunnel production
    behavior. It binds the transport-neutral MCP dispatcher, the durable
    Operation Registry, schema-liveness lease, and private loopback adapter so
    their lifecycle can be acceptance-tested before any persistent cutover.
    """

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
        capabilities: tuple[str, ...] | list[str] = (),
        state_root: Path | None = None,
        boot_id: str | None = None,
        containment_identity: str,
        quiescent_previous_boot_ids: Iterable[str] = (),
        settlement_capability: TransportSettlementCapability | None = None,
    ) -> None:
        resolved_boot_id = secrets.token_hex(16) if boot_id is None else boot_id
        if not isinstance(resolved_boot_id, str) or not _BOOT_ID_RE.fullmatch(resolved_boot_id):
            raise ValueError("boot_id must be 32 lowercase hex characters")
        if not isinstance(containment_identity, str) or not containment_identity:
            raise ValueError("containment_identity must be a non-empty string")

        self.boot_id = resolved_boot_id
        self.state_root = (
            user_config_root()
            if state_root is None
            else user_config_root(test_root=Path(state_root))
        )
        self.registry = OperationRegistry(self.state_root)
        self._closed = False
        self._started = False
        self._schema_lease = None
        self.runtime: ToolRuntime | None = None
        self.mcp: McpServer | None = None
        self.scheduler: McpRequestScheduler | None = None
        self.admission: GenerationAdmission | None = None
        self.http: PrivateMcpHttpServer | None = None

        lease = self.registry.register_schema_liveness(
            boot_id=self.boot_id,
            binary_version=__version__,
            process_role="runtime",
            containment_identity=containment_identity,
            readable_schema_min=min(REGISTRY_COMPAT_SCHEMA_VERSIONS),
            readable_schema_max=max(REGISTRY_COMPAT_SCHEMA_VERSIONS),
            writable_schema_min=REGISTRY_SCHEMA_VERSION,
            writable_schema_max=REGISTRY_SCHEMA_VERSION,
        )
        self._schema_lease = lease
        try:
            self.recovered_previous_boot = self.registry.recover_for_boot(
                self.boot_id,
                quiescent_boot_ids=quiescent_previous_boot_ids,
            )
            self.recovery_required = self._compute_recovery_required()

            runtime = ToolRuntime.from_roots(
                list(roots),
                read_only=read_only,
                allow_tasks=allow_tasks,
                capabilities=capabilities,
                recovery_context=RecoveryAdmissionContext(
                    transport_mode="persistent_tunnel",
                    settlement=settlement_capability or TransportSettlementCapability.unproven(),
                ),
                operation_registry=self.registry,
                boot_id=resolved_boot_id,
            )
            self.runtime = runtime
            self.mcp = McpServer(runtime)
            self.scheduler = McpRequestScheduler(self.mcp.dispatch)
            self.admission = GenerationAdmission()
            self.http = PrivateMcpHttpServer(
                self.scheduler.dispatch_sync,
                self.admission,
                control_classifier=is_recovery_control_request,
            )
        except BaseException:
            scheduler = self.scheduler
            if scheduler is not None:
                try:
                    scheduler.close()
                except BaseException:
                    pass
            runtime = self.runtime
            if runtime is not None:
                try:
                    runtime.close()
                except BaseException:
                    pass
            try:
                lease.close()
            finally:
                self._schema_lease = None
            raise

    def _compute_recovery_required(self) -> bool:
        if self.registry.recovery_only:
            return True
        return any(receipt.protected for receipt in self.registry.list_all_for_operator())

    @property
    def port(self) -> int:
        http = self.http
        if http is None:
            raise RuntimeHostError("RuntimeHost is not initialized")
        return http.port

    def start(self) -> None:
        if self._closed:
            raise RuntimeHostError("RuntimeHost is closed")
        if self._started:
            return
        http = self.http
        if http is None:
            raise RuntimeHostError("RuntimeHost private transport is unavailable")
        http.start()
        self._started = True

    def register_generation(
        self,
        token: str,
        *,
        generation: int,
        state: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeHostError("RuntimeHost is closed")
        admission = self.admission
        if admission is None:
            raise RuntimeHostError("RuntimeHost generation admission is unavailable")

        self.recovery_required = self._compute_recovery_required()
        requested_state = (
            "control_only" if self.recovery_required else "active"
            if state is None
            else state
        )
        if state is not None:
            requested_state = state
        if self.recovery_required and requested_state == "active":
            raise RuntimeRecoveryBlocked(
                "protected unresolved operations require recovery-control-only admission"
            )
        if requested_state not in {"control_only", "active"}:
            raise ValueError("new generation state must be control_only or active")
        admission.register_generation(token, generation=generation, state=requested_state)

    def refresh_recovery_required(self) -> bool:
        self.recovery_required = self._compute_recovery_required()
        return self.recovery_required

    def activate_generation_after_recovery(self, token: str) -> dict[str, object]:
        """Promote one existing control-only generation after explicit recovery.

        Reconciliation is performed by the original owner/operator against the
        durable Registry. This method owns only the RuntimeHost admission fence:
        it re-reads protected state before activation, promotes the exact token,
        then re-checks the Registry and rolls the generation back to
        ``control_only`` if blocking state appeared during the handoff.
        """
        if self._closed:
            raise RuntimeHostError("RuntimeHost is closed")
        admission = self.admission
        if admission is None:
            raise RuntimeHostError("RuntimeHost generation admission is unavailable")
        if self.refresh_recovery_required():
            raise RuntimeRecoveryBlocked(
                "protected unresolved operations still require recovery-control-only admission"
            )
        admission.set_state(token, "active")
        if self.refresh_recovery_required():
            admission.set_state(token, "control_only")
            raise RuntimeRecoveryBlocked(
                "protected unresolved operations appeared while activating the generation"
            )
        return admission.snapshot(token)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        http = self.http
        scheduler = self.scheduler
        runtime = self.runtime
        lease = self._schema_lease
        self._schema_lease = None
        try:
            if http is not None:
                http.close()
        finally:
            try:
                if scheduler is not None:
                    scheduler.close()
            finally:
                try:
                    if runtime is not None:
                        runtime.close()
                finally:
                    if lease is not None:
                        lease.close()

    def __enter__(self) -> "RuntimeHost":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class StdioSupervisor:
    """Phase-0 direct-stdio recovery supervisor.

    The supervisor shares the canonical Operation Registry authority with
    RuntimeHost, registers schema liveness as a stdio supervisor, performs
    boot recovery before ToolRuntime construction, and gates direct-stdio MCP
    dispatch while protected receipts remain unresolved.
    """

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
        capabilities: tuple[str, ...] | list[str] = (),
        state_root: Path | None = None,
        boot_id: str | None = None,
        containment_identity: str | None = None,
        quiescent_previous_boot_ids: Iterable[str] = (),
    ) -> None:
        resolved_boot_id = secrets.token_hex(16) if boot_id is None else boot_id
        if not isinstance(resolved_boot_id, str) or not _BOOT_ID_RE.fullmatch(resolved_boot_id):
            raise ValueError("boot_id must be 32 lowercase hex characters")
        resolved_containment = containment_identity or f"stdio-supervisor:{resolved_boot_id}"
        if not isinstance(resolved_containment, str) or not resolved_containment:
            raise ValueError("containment_identity must be a non-empty string")

        self.boot_id = resolved_boot_id
        self.state_root = (
            user_config_root()
            if state_root is None
            else user_config_root(test_root=Path(state_root))
        )
        self.registry = OperationRegistry(self.state_root)
        self._closed = False
        self._schema_lease = None
        self.runtime: ToolRuntime | None = None
        self.mcp: McpServer | None = None

        lease = self.registry.register_schema_liveness(
            boot_id=self.boot_id,
            binary_version=__version__,
            process_role="stdio-supervisor",
            containment_identity=resolved_containment,
            readable_schema_min=min(REGISTRY_COMPAT_SCHEMA_VERSIONS),
            readable_schema_max=max(REGISTRY_COMPAT_SCHEMA_VERSIONS),
            writable_schema_min=REGISTRY_SCHEMA_VERSION,
            writable_schema_max=REGISTRY_SCHEMA_VERSION,
        )
        self._schema_lease = lease
        try:
            self.recovered_previous_boot = self.registry.recover_for_boot(
                self.boot_id,
                quiescent_boot_ids=quiescent_previous_boot_ids,
            )
            self.recovery_required = self._compute_recovery_required()
            runtime = ToolRuntime.from_roots(
                list(roots),
                read_only=read_only,
                allow_tasks=allow_tasks,
                capabilities=capabilities,
                recovery_context=RecoveryAdmissionContext.direct_stdio(),
                operation_registry=self.registry,
                boot_id=resolved_boot_id,
            )
            self.runtime = runtime
            self.mcp = McpServer(runtime, request_admission=self._admit_request)
        except BaseException:
            runtime = self.runtime
            if runtime is not None:
                try:
                    runtime.close()
                except BaseException:
                    pass
            try:
                lease.close()
            finally:
                self._schema_lease = None
            raise

    def _compute_recovery_required(self) -> bool:
        if self.registry.recovery_only:
            return True
        return any(receipt.protected for receipt in self.registry.list_all_for_operator())

    def refresh_recovery_required(self) -> bool:
        self.recovery_required = self._compute_recovery_required()
        return self.recovery_required

    def _admit_request(self, request: dict) -> bool:
        if not self.refresh_recovery_required():
            return True
        return is_recovery_control_request(request)

    def serve(self) -> None:
        if self._closed:
            raise RuntimeHostError("StdioSupervisor is closed")
        mcp = self.mcp
        if mcp is None:
            raise RuntimeHostError("StdioSupervisor MCP server is unavailable")
        mcp.serve()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        runtime = self.runtime
        lease = self._schema_lease
        self._schema_lease = None
        try:
            if runtime is not None:
                runtime.close()
        finally:
            if lease is not None:
                lease.close()

    def __enter__(self) -> "StdioSupervisor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
