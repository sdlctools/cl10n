from __future__ import annotations

import sys
from pathlib import Path

from cl10n.core.utils import ast_to_markdown, markdown_to_ast




def main() -> None:
    if len(sys.argv) == 2:
        fpath = sys.argv[1]
        if not Path.is_file(fpath):
            raise
        md_content = open(fpath, encoding="utf-8").read()
        md_content_normalized = ast_to_markdown(markdown_to_ast(md_content))
        with open(f"norm_{fpath}",'w', encoding="utf-8") as f:
            f.write(md_content_normalized)

    else:
        raise



if __name__ == "__main__":
    main()
