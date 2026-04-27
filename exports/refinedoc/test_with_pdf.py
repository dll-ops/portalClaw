from pathlib import Path
import fitz
from refinedoc.refined_document import RefinedDocument

pdf_path = Path('/home/dll/.openclaw/media/inbound/A_single_cell_atlas_of_Plasmodium_falciparum_transmission_th---59c0e78f-3d7c-43b6-8a46-445347bd9b66.pdf')
out_dir = Path('/home/dll/refinedoc/output')
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / (pdf_path.stem + '.txt')

doc = fitz.open(pdf_path)
pages = []
for page in doc:
    pages.append(page.get_text('text').split('\n'))

rd = RefinedDocument(content=pages)
body_pages = rd.body
text = '\n\n'.join('\n'.join(page) for page in body_pages if page)
out_path.write_text(text.strip() + '\n', encoding='utf-8')
print(out_path)
