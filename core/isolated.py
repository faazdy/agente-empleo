"""
Ejecuta una corrutina en un hilo con su PROPIO bucle de eventos.

Por qué: Playwright lanza su driver con subprocesos asyncio, y en Windows eso solo funciona con
ProactorEventLoop. Si el servidor se lanzó con `uvicorn --reload` (o con varios procesos),
uvicorn usa SelectorEventLoop y Playwright falla con `NotImplementedError` (mensaje vacío).
Aislar el trabajo del navegador en su propio bucle lo hace independiente de cómo se arranque el servidor.
"""
import asyncio
import sys
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def _new_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


async def run_isolated(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Corre `coro_factory()` en otro hilo, con bucle propio, y devuelve su resultado (o su excepción)."""

    def runner() -> T:
        loop = _new_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    return await asyncio.get_running_loop().run_in_executor(None, runner)


def describe_error(e: BaseException) -> str:
    """Texto de error que nunca sale vacío (p. ej. NotImplementedError() o TimeoutError())."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__
