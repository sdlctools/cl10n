from utils import markdown_to_xml, ast_to_markdown, markdown_to_ast





if __name__ == "__main__":
    mdpath =  "../md/skills/_shared/templates/review-report.md"
    mdpath_output = f"{mdpath}.xml"

    with open(mdpath, 'r', encoding='utf-8') as f:
        raw_markdown = f.read()

    normalized_markdown = ast_to_markdown(markdown_to_ast(raw_markdown))
    
    data = markdown_to_xml(normalized_markdown)


    with open(mdpath_output, 'w', encoding='utf-8') as f:
        f.write(data)
    
 
