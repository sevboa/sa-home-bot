from __future__ import annotations

from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo


class StubService:
    def __init__(
        self,
        node: str = "winpc",
        *,
        service: str = "node",
        version: str = "0.45.0",
        endpoints: tuple[str, ...] = (),
        node_kind: str = "",
        capabilities: tuple[str, ...] = (),
        actions: tuple[ActionSpec, ...] = (ActionSpec(id="noop", title="Ничего"),),
        state: dict | None = None,
    ) -> None:
        self.node = node
        self.service = service
        self.version = version
        self.endpoints = endpoints
        self.node_kind = node_kind
        self.capabilities = capabilities
        self.actions = actions
        self.state = state if state is not None else {"node": node}

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(
                node=self.node,
                service=self.service,
                version=self.version,
                endpoints=self.endpoints,
                node_kind=self.node_kind,
            ),
            capabilities=self.capabilities,
            actions=self.actions,
        )

    async def get_state(self) -> dict:
        return self.state

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True}

