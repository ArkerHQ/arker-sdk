"""Derive an async test program when every call on named targets is awaitable."""

from __future__ import annotations

import ast
from textwrap import indent


def async_program(source: str, *, rename: dict[str, str], targets: set[str]) -> str:
    class AsyncCalls(ast.NodeTransformer):
        def visit_Name(self, node):
            node.id = rename.get(node.id, node.id)
            return node

        def visit_alias(self, node):
            node.name = rename.get(node.name, node.name)
            return node

        def visit_Call(self, node):
            node = self.generic_visit(node)
            target = node.func
            while isinstance(target, ast.Attribute):
                target = target.value
            if isinstance(target, ast.Name) and target.id in targets:
                return ast.copy_location(ast.Await(value=node), node)
            return node

    tree = AsyncCalls().visit(ast.parse(source))
    return "async def program():\n" + indent(ast.unparse(ast.fix_missing_locations(tree)), "    ")


def trace_program(source, filename, namespace, roots, called):
    """Compile named API expressions with observations at execution, not discovery."""
    import inspect

    call_hook, read_hook = "__compat_observe_call__", "__compat_observe_read__"
    tree = ast.parse(source, filename)
    if any(isinstance(node, ast.Name) and node.id in {call_hook, read_hook} for node in ast.walk(tree)):
        raise ValueError("Program uses a reserved observation name")

    def invoke(api, function, *args, **kwargs):
        if inspect.iscoroutinefunction(function):

            async def awaited():
                called.add(api)
                return await function(*args, **kwargs)

            return awaited()
        called.add(api)
        return function(*args, **kwargs)

    def read(api, receiver, name):
        value = getattr(receiver, name)
        if not callable(value):
            called.add(api)
        return value

    def api_path(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in roots and parts:
            return ".".join([roots[node.id], *reversed(parts)])
        return None

    class Observations(ast.NodeTransformer):
        def visit_Call(self, node):
            api = api_path(node.func)
            if api is None:
                return self.generic_visit(node)
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id=call_hook, ctx=ast.Load()),
                    args=[ast.Constant(api), node.func, *[self.visit(arg) for arg in node.args]],
                    keywords=[self.visit(keyword) for keyword in node.keywords],
                ),
                node,
            )

        def visit_Attribute(self, node):
            api = api_path(node)
            if api is None or not isinstance(node.ctx, ast.Load):
                return self.generic_visit(node)
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id=read_hook, ctx=ast.Load()),
                    args=[ast.Constant(api), node.value, ast.Constant(node.attr)],
                    keywords=[],
                ),
                node,
            )

    namespace.update({call_hook: invoke, read_hook: read})
    return compile(ast.fix_missing_locations(Observations().visit(tree)), filename, "exec")
