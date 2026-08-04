from markdown_it import MarkdownIt
from mdformat.renderer import MDRenderer
import mdformat.plugins




def make_parser() -> MarkdownIt:
    """A parser configured exactly as the whole pipeline expects it.

    One function because every consumer must agree: the planner hashes what
    this parses, and reassembly re-parses translated inline content with the
    *same* rules (`parse_inline` below). Two copies of this setup drifting
    apart would change token streams under the hashes.
    """
    # 1. Initialize parser and the required plugin list
    md = MarkdownIt("gfm-like2")
    md.options["linkify"] = False
    md.options["parser_extension"] = []

    # 2. Dynamically load EVERY installed mdformat plugin (GFM, tables, frontmatter, etc.)
    for plugin in mdformat.plugins.PARSER_EXTENSIONS.values():
        if plugin not in md.options["parser_extension"]:
            md.options["parser_extension"].append(plugin)
            plugin.update_mdit(md)

    return md


def markdown_to_ast(raw_markdown) -> str:
    """
    Parses Markdown into AST tokens.
    """
    # 3. Generate the AST tokens
    tokens = make_parser().parse(raw_markdown)
    return tokens


def parse_inline(text: str) -> list:
    """Tokenize `text` as *inline* markdown — no block parsing at all.

    Reassembly's splice: a translated segment is inline content by definition,
    so a translation that happens to start with `- ` or `1. ` must stay one
    paragraph rather than becoming a list. `parseInline` is what guarantees
    that; block-parsing the string and picking the inline token out would not.
    Returns the children of the single `inline` token it produces.
    """
    return make_parser().parseInline(text, {})[0].children or []


def ast_to_markdown(tokens) -> str:
    """
    Parses Markdown into AST tokens.
    """
    md = make_parser()

    # 3. Generate the AST tokens

    options = dict(md.options)
     #options["mdformat"] = {"wrap": "keep"}
     #options["mdformat"] = {"wrap": 80}
 
    options["mdformat"] = {
         "number": True,  # Enables consecutive numbering for ordered lists
         "wrap": "keep",  # Retains your semantic line breaks
         "compact_tables": True,
         #"linkify" : False
    }
 
 
     
    # NOTE: Do NOT overwrite options["parser_extension"] here!

    # 5. Render AST directly back to Markdown (NO HTML!)
    renderer = MDRenderer()
    final_markdown = renderer.render(tokens, options, {})

    return final_markdown


def normalize_markdown(src, dst) -> str:
    with open(src, "r", encoding="utf-8") as f:
        raw_markdown = f.read()

    final_markdown = ast_to_markdown(markdown_to_ast(raw_markdown))
    #final_markdown = make_tables_compact(final_markdown)

    # 6. Save to disk
    with open(dst, "w", encoding="utf-8") as f:
        f.write(final_markdown)


def generate_ast_tree(src):
    with open(src, "r", encoding="utf-8") as f:
        raw_markdown = f.read()

    tokens = markdown_to_ast(raw_markdown)
    
    lines = []
    
    def process_tokens(token_list, start_depth):
        depth = start_depth
        
        for t in token_list:
            # If it's a closing token, we decrease the indentation first
            if t.type.endswith('_close'):
                depth -= 1
                
            indent = "    " * depth
            
            # 1. Format the node's base type and tag
            node_label = f"<{t.type}>"
            if t.tag:
                node_label += f" (tag: {t.tag})"
            
            # 2. Add content preview for text nodes
            if t.type in ['text', 'code_inline', 'fence', 'html_block'] and t.content:
                # Clean up newlines and truncate long strings for the visualizer
                preview = t.content.replace('\n', '\\n')
                if len(preview) > 35:
                    preview = preview[:32] + "..."
                node_label += f"  →  '{preview}'"
                
            lines.append(f"{indent}├── {node_label}")
            
            # If it's an opening token, we increase indentation for the next tokens
            if t.type.endswith('_open'):
                depth += 1
                
            # 3. If the token has children (like the 'inline' token), process them deeper
            if t.children:
                process_tokens(t.children, depth + 1)

    process_tokens(tokens, 0)
    return "\n".join(lines)





from xml.etree.ElementTree import Element, SubElement, tostring
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from xml.etree.ElementTree import Element, SubElement
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom


def node_to_xml(ast_node, xml_parent=None):
    # Check node type safely
    is_root = getattr(ast_node, "type", "") == "root"
    
    # Determine basic node category wrapper
    node_category = "leaf" if not ast_node.children else "block"
    if getattr(ast_node, "type", "") == "inline":
        node_category = "inline"
    elif is_root:
        node_category = "root"

    # Build attributes safely by avoiding root node token lookups
    attrs = {
        "type": str(getattr(ast_node, "type", "unknown"))
    }
    
    # Root node crashes on tag, level, map, and content, so skip them
    if not is_root:
        attrs["tag"] = str(getattr(ast_node, "tag", "") or "")
        attrs["level"] = str(getattr(ast_node, "level", "0"))
        
        # Safely fetch map and content properties
        node_map = getattr(ast_node, "map", None)
        node_content = getattr(ast_node, "content", None)
        
        if node_map:
            attrs["line_start"] = str(node_map[0])
            attrs["line_end"] = str(node_map[1])
        if node_content:
            attrs["content"] = str(node_content)
    else:
        # Default fallback values for the root wrapper
        attrs["tag"] = ""
        attrs["level"] = "0"

    # Create the XML Element node
    if xml_parent is None:
        el = Element(node_category, attrib=attrs)
    else:
        el = SubElement(xml_parent, node_category, attrib=attrs)
    
    # Recurse through children
    for child in ast_node.children:
        node_to_xml(child, xml_parent=el)
        
    return el




def markdown_to_xml(raw_markdown):
    tokens = markdown_to_ast(raw_markdown)
    ast_tree = SyntaxTreeNode(tokens)

    xml_root = node_to_xml(ast_tree)

    # 2. Convert the object tree to a raw byte string
    raw_bytes = ET.tostring(xml_root, encoding="utf-8")

    # 3. Use minidom to parse the raw string and format it nicely
    parsed_dom = minidom.parseString(raw_bytes)
    pretty_xml = parsed_dom.toprettyxml(indent="  ")

    # 4. Print the entire tree to your terminal
    #print(pretty_xml)

    return pretty_xml

    