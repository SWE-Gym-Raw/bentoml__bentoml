from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import functools
import inspect
import typing as t
from http import HTTPStatus
from urllib.parse import urljoin
from urllib.parse import urlparse

import anyio

from ...exceptions import BentoMLException
from ..utils.uri import uri_to_path

if t.TYPE_CHECKING:
    import yarl
    from aiohttp import ClientResponse
    from aiohttp import ClientSession

    from ..runner import Runner
    from .models import IODescriptor

    T = t.TypeVar("T", bound="BaseClient")


@dataclasses.dataclass(frozen=True)
class ClientEndpoint:
    name: str
    route: str
    doc: str | None = None
    input: dict[str, t.Any] = dataclasses.field(default_factory=dict)
    output: dict[str, t.Any] = dataclasses.field(default_factory=dict)
    input_spec: type[IODescriptor] | None = None
    output_spec: type[IODescriptor] | None = None
    stream_output: bool = False


@dataclasses.dataclass
class BaseClient(abc.ABC):
    url: str
    endpoints: dict[str, ClientEndpoint] = dataclasses.field(default_factory=dict)
    media_type: str = "application/json"
    timeout: int | None = None
    max_connections: int | None = None
    _client: ClientSession | None = dataclasses.field(init=False, default=None)
    _loop: asyncio.AbstractEventLoop | None = dataclasses.field(
        init=False, default=None
    )

    def __post_init__(self) -> None:
        from .serde import ALL_SERDE

        self.serde = ALL_SERDE[self.media_type]()
        for name in self.endpoints:
            setattr(self, name, self._make_method(name))

        if self.max_connections is not None:
            self._limiter: t.AsyncContextManager[t.Any] = asyncio.Semaphore(
                self.max_connections
            )
        else:
            self._limiter = contextlib.nullcontext()

    def _make_method(self, name: str) -> t.Callable[..., t.Any]:
        endpoint = self.endpoints[name]

        def method(**kwargs: t.Any) -> t.Any:
            return self.call(name, kwargs)

        method.__doc__ = endpoint.doc
        if endpoint.input_spec is not None:
            method.__annotations__ = endpoint.input_spec.__annotations__
            method.__signature__ = endpoint.input_spec.__signature__
        return method

    def _get_client(self) -> ClientSession:
        import aiohttp
        from opentelemetry.instrumentation.aiohttp_client import create_trace_config

        from ..container import BentoMLContainer

        if (
            self._loop is None
            or self._client is None
            or self._client.closed
            or self._loop.is_closed()
        ):
            self._loop = asyncio.get_event_loop()

            def strip_query_params(url: yarl.URL) -> str:
                return str(url.with_query(None))

            parsed = urlparse(self.url)
            client_kwargs = {
                "loop": self._loop,
                "trust_env": True,
                "trace_configs": [
                    create_trace_config(
                        # Remove all query params from the URL attribute on the span.
                        url_filter=strip_query_params,
                        tracer_provider=BentoMLContainer.tracer_provider.get(),
                    )
                ],
            }
            if self.timeout:
                client_kwargs["timeout"] = aiohttp.ClientTimeout(total=self.timeout)
            if parsed.scheme == "file":
                path = uri_to_path(self.url)
                conn = aiohttp.UnixConnector(
                    path=path,
                    loop=self._loop,
                    limit=800,  # TODO(jiang): make it configurable
                    keepalive_timeout=1800.0,
                )
                self._client = aiohttp.ClientSession(connector=conn, **client_kwargs)
            elif parsed.scheme == "tcp":
                url = f"http://{parsed.netloc}"
                self._client = aiohttp.ClientSession(url, **client_kwargs)
            else:
                self._client = aiohttp.ClientSession(self.url, **client_kwargs)
        return self._client

    @classmethod
    def for_runner(
        cls: type[T], runner: Runner, *, media_type: str = "application/json"
    ) -> T:
        """Create a client instance from a runner with schema information.

        Args:
            runner: The runner to create client from.
            media_type: The media type to use for serialization. Defaults to
                "application/json".
        """
        from ..container import BentoMLContainer

        runner_bind_map = BentoMLContainer.remote_runner_mapping.get()
        if runner.name not in runner_bind_map:
            raise RuntimeError(
                f"Runner {runner.name} must be started as remote runner to use this method"
            )

        url = runner_bind_map[runner.name]
        routes: dict[str, ClientEndpoint] = {}
        for meth in runner.runner_methods:
            if meth.config.input_spec is None or meth.config.output_spec is None:
                raise RuntimeError(
                    f"Runner method {meth.name} must have input_spec and output_spec"
                )
            routes[meth.name] = ClientEndpoint(
                name=meth.name,
                route=f"/{meth.name}",
                doc=meth.doc,
                input=meth.config.input_spec.model_json_schema(),
                output=meth.config.output_spec.model_json_schema(),
                input_spec=meth.config.input_spec,
                output_spec=meth.config.output_spec,
                stream_output=meth.config.is_stream,
            )
        runner_cfg = BentoMLContainer.runners_config.get()
        if runner.name in runner_cfg:
            timeout = runner_cfg[runner.name].get("traffic", {})["timeout"]
        else:
            timeout = runner_cfg.get("traffic", {})["timeout"]
        max_connections = (
            BentoMLContainer.api_server_config.max_runner_connections.get()
        )
        return cls(
            url,
            routes,
            media_type=media_type,
            timeout=timeout,
            max_connections=max_connections,
        )

    @classmethod
    def for_url(cls: type[T], url: str, *, media_type: str = "application/json") -> T:
        """Create a client instance from a URL.

        Args:
            url: The URL of the BentoML service.
            media_type: The media type to use for serialization. Defaults to
                "application/json".

        .. note::

            The client created with this method can only return primitive types without a model.
        """
        import requests

        schema_url = urljoin(url, "/schema.json")
        resp = requests.get(schema_url)

        if not resp.ok:
            raise RuntimeError(f"Failed to fetch schema from {schema_url}")
        routes: dict[str, ClientEndpoint] = {}
        for route in resp.json()["routes"]:
            routes[route["name"]] = ClientEndpoint(
                name=route["name"],
                route=route["route"],
                input=route["input"],
                output=route["output"],
                doc=route.get("doc"),
                stream_output=route["output"].get("is_stream", False),
            )
        return cls(url, routes, media_type=media_type)

    async def _call(
        self,
        name: str,
        params: dict[str, t.Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> t.Any:
        try:
            endpoint = self.endpoints[name]
        except KeyError:
            raise BentoMLException(f"Endpoint {name} not found") from None
        data = await self._prepare_request(endpoint, params)
        resp = await self._request(endpoint.route, data, headers=headers)
        if endpoint.stream_output:
            return self._parse_response_stream(endpoint, resp)
        else:
            return await self._parse_response(endpoint, resp)

    async def _request(
        self, url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> ClientResponse:
        req_headers = {"Content-Type": self.media_type}
        if headers is not None:
            req_headers.update(headers)
        async with self._limiter:
            resp = await self._get_client().post(url, data=data, headers=req_headers)
        if not resp.ok:
            raise BentoMLException(
                f"Error making request: {resp.status}: {await resp.text(errors='ignore')}",
                error_code=HTTPStatus(resp.status),
            )
        return resp

    async def _prepare_request(self, endpoint: ClientEndpoint, kwargs: t.Any) -> bytes:
        if endpoint.input_spec is not None:
            model = endpoint.input_spec(**kwargs)
            return self.serde.serialize_model(model)
        else:
            params = set(endpoint.input["properties"].keys())
            non_exist_args = set(kwargs.keys()) - set(params)
            if non_exist_args:
                raise TypeError(
                    f"Arguments not found in endpoint {endpoint.name}: {non_exist_args}"
                )
            required = set(endpoint.input.get("required", []))
            missing_args = set(required) - set(kwargs.keys())
            if missing_args:
                raise TypeError(
                    f"Missing required arguments in endpoint {endpoint.name}: {missing_args}"
                )
            return self.serde.serialize(kwargs)

    def _deserialize_output(self, data: bytes, endpoint: ClientEndpoint) -> t.Any:
        if endpoint.output["type"] == "string":
            return data.decode("utf-8")
        elif endpoint.output["type"] == "bytes":
            return data
        if endpoint.output_spec is None:
            return self.serde.deserialize(data)
        else:
            return self.serde.deserialize_model(data, endpoint.output_spec)

    async def _parse_response(
        self, endpoint: ClientEndpoint, resp: ClientResponse
    ) -> t.Any:
        data = await resp.read()
        if endpoint.output_spec is not None:
            return self.serde.deserialize_model(data, endpoint.output_spec)
        else:
            return self._deserialize_output(data, endpoint)

    async def _parse_response_stream(
        self, endpoint: ClientEndpoint, resp: ClientResponse
    ) -> t.AsyncGenerator[t.Any, None]:
        buffer = bytearray()
        async for data, eoc in resp.content.iter_chunks():
            buffer.extend(data)
            if eoc:
                yield self._deserialize_output(bytes(buffer), endpoint)
                buffer.clear()

    async def close(self) -> None:
        if self._client is not None and not self._client.closed:
            await self._client.close()

    @abc.abstractmethod
    def call(self, name: str, params: dict[str, t.Any]) -> t.Any:
        """Call a service method by its name.
        It takes the same arguments as the service method.
        """
        ...


class Client(BaseClient):
    """A synchronous client for BentoML service.

    Example:

        with Client.for_url("http://localhost:3000") as client:
            resp = client.call("classify", input_series=[[1,2,3,4]])
            assert resp == [0]
            # Or using named method directly
            resp = client.classify(input_series=[[1,2,3,4]])
            assert resp == [0]
    """

    def call(
        self,
        name: str,
        params: dict[str, t.Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> t.Any:
        from ..utils import async_gen_to_sync

        res = anyio.run(functools.partial(self._call, headers=headers), name, params)
        if inspect.isasyncgen(res):
            return async_gen_to_sync(res)
        return res

    def __enter__(self) -> BaseClient:
        return self

    def __exit__(self, exc_type: t.Any, exc: t.Any, tb: t.Any) -> None:
        return anyio.run(self.close)


class AsyncClient(BaseClient):
    """An asynchronous client for BentoML service.

    Example:

        async with AsyncClient.for_url("http://localhost:3000") as client:
            resp = await client.call("classify", input_series=[[1,2,3,4]])
            assert resp == [0]
            # Or using named method directly
            resp = await client.classify(input_series=[[1,2,3,4]])
            assert resp == [0]

    .. note::

        If the endpoint returns an async generator, it should be awaited before iterating.

        Example:

            resp = await client.stream(prompt="hello")
            async for data in resp:
                print(data)
    """

    async def call(
        self,
        name: str,
        params: dict[str, t.Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> t.Any:
        return await self._call(name, params, headers=headers)

    async def __aenter__(self) -> BaseClient:
        return self

    async def __aexit__(self, exc_type: t.Any, exc: t.Any, tb: t.Any) -> None:
        await self.close()