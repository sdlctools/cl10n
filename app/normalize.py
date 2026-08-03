from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from markdown_it.tree import SyntaxTreeNode

from utils import ast_to_markdown, markdown_to_ast




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
