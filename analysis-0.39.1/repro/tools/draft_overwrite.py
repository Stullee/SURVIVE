"""TOOLS: draft with mode overwrite replaces a text file and keeps no earlier version (restore then has nothing)."""

from harness import agent, call, context, rows, show

from app.agent import tools

a, _ = agent()
ctx = context(a)
ctx.draft = lambda brief, sources: tools.Drafted("# Interior\n\n(header only)\n", False, 1_000)
BOOK = "# Haushaltsbuch 2027 interior\n\n" + "Week table ...\n" * 1500
ctx.workspace.write("books/interior.md", BOOK)
show("draft overwrite", call(ctx, "draft", {"path": "books/interior.md", "brief": "Rewrite the header.", "mode": "overwrite"}))
print("now:", len(ctx.workspace.read("books/interior.md").encode()), "bytes, was", len(BOOK.encode()))
print("versions:", rows(a, "SELECT COUNT(*) AS n FROM workspace_versions")[0]["n"])
show("restore", call(ctx, "workspace_write", {"path": "books/interior.md", "mode": "restore"}))
