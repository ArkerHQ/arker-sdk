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
