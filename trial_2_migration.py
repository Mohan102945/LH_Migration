
"""Trial 2 Migration.ipynb

import streamlit as st
import os
import tempfile
import json
from zipfile import ZipFile
from io import BytesIO
import time

# Adobe PDF Services SDK
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdfjobs.jobs.extract_pdf_job import ExtractPDFJob
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_element_type import ExtractElementType
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_renditions_element_type import ExtractRenditionsElementType
from adobe.pdfservices.operation.pdfjobs.params.extract_pdf.extract_pdf_params import ExtractPDFParams
from adobe.pdfservices.operation.pdfjobs.result.extract_pdf_result import ExtractPDFResult

# Core comparison logic
import fitz
from sentence_transformers import SentenceTransformer, util
from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel

model = SentenceTransformer('all-mpnet-base-v2')
clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

st.set_page_config(layout="centered")
st.title("📑 Semantic PDF Comparison & KO Highlighter")

client_id = st.text_input("🔐 Adobe PDF Services Client ID", type="password")
client_secret = st.text_input("🔐 Adobe PDF Services Client Secret", type="password")

pdf1 = st.file_uploader("📄 Upload PDF 1", type="pdf", key="pdf1")
pdf2 = st.file_uploader("📄 Upload PDF 2", type="pdf", key="pdf2")

@st.cache_data(show_spinner=False)
def extract_pdf_json(pdf_file, client_id, client_secret):
    credentials = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
    pdf_services = PDFServices(credentials=credentials)
    input_stream = pdf_file.read()
    asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)
    params = ExtractPDFParams(
        elements_to_extract=[ExtractElementType.TEXT, ExtractElementType.TABLES],
        elements_to_extract_renditions=[ExtractRenditionsElementType.FIGURES, ExtractRenditionsElementType.TABLES]
    )
    job = ExtractPDFJob(input_asset=asset, extract_pdf_params=params)
    location = pdf_services.submit(job)
    for attempt in range(3):
        try:
            result = pdf_services.get_job_result(location, ExtractPDFResult)
            stream_asset: StreamAsset = pdf_services.get_content(result.get_result().get_resource())
            break
        except Exception as e:
            if attempt < 2:
                st.warning(f"⏳ Retry {attempt+1}/3: Adobe service timeout. Retrying in 5 seconds...")
                time.sleep(5)
            else:
                st.error("🚫 Adobe PDF Services failed after 3 attempts. Please try again later.")
                raise e
    zip_bytes = stream_asset.get_input_stream()

    temp_dir = tempfile.mkdtemp()
    with ZipFile(BytesIO(zip_bytes)) as z:
        z.extractall(temp_dir)
    with open(os.path.join(temp_dir, "structuredData.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    return data, temp_dir

def simple_tokenize(text):
    return text.replace("\n", " ").split()

def ko_compare_tokens(tokens1, tokens2):
    changes = []
    i = j = 0
    while i < len(tokens1) and j < len(tokens2):
        if tokens1[i] == tokens2[j]:
            i += 1
            j += 1
        else:
            found = False
            for k in range(j+1, len(tokens2)):
                if tokens1[i] == tokens2[k]:
                    changes.append(("added", j, tokens2[j:k]))
                    j = k
                    found = True
                    break
            if not found:
                changes.append(("deleted", i, [tokens1[i]]))
                i += 1
    for rem in tokens1[i:]:
        changes.append(("deleted", i, [rem]))
    for rem in tokens2[j:]:
        changes.append(("added", j, [rem]))
    return changes

def cosine_sim(a, b):
    emb1 = model.encode(a, convert_to_tensor=True)
    emb2 = model.encode(b, convert_to_tensor=True)
    return util.cos_sim(emb1, emb2).item()

def adobe_to_fitz_bbox(bbox, page_height):
    left, bottom, right, top = bbox
    return fitz.Rect(left, page_height - top, right, page_height - bottom)

def highlight_pdf(pdf_path, elements, changes, out_path, color):
    doc = fitz.open(pdf_path)

    for ch in changes:
        tag, idx, tokens = ch
        for el in elements:
            if "Text" in el and any(t in el["Text"] for t in tokens):
                bbox = el.get("Bounds")
                if bbox and "Page" in el:
                    page_num = el["Page"]
                    page = doc[page_num]
                    rect = adobe_to_fitz_bbox(bbox, page.rect.height)
                    shape = page.new_shape()
                    shape.draw_rect(rect)
                    shape.finish(color=color, width=1.5)
                    shape.commit()

    # Decide whether to use incremental save
    try:
        if os.path.samefile(pdf_path, out_path):
            # Saving back to the same file → must use incremental
            doc.save(out_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        else:
            # Saving to a new file → normal save is allowed
            doc.save(out_path)
    except Exception as e:
        # In case samefile fails on virtual paths or fallback needed
        doc.save(out_path)

    return out_path


def extract_figure_paths(base_path):
    fig_dir = os.path.join(base_path, "figures")
    if not os.path.exists(fig_dir):
        return []
    return [os.path.join(fig_dir, f) for f in os.listdir(fig_dir) if f.endswith(".png")]

def compare_figures(figs1, figs2, threshold=0.8):
    if not figs1 or not figs2:
        return [], figs1, figs2  # No matches possible

    images1 = [Image.open(f).convert("RGB") for f in figs1]
    images2 = [Image.open(f).convert("RGB") for f in figs2]

    clip_model.eval()

    with torch.no_grad():
        emb1 = clip_model.get_image_features(**clip_processor(images=images1, return_tensors="pt", padding=True))
        emb2 = clip_model.get_image_features(**clip_processor(images=images2, return_tensors="pt", padding=True))

        emb1 = emb1 / emb1.norm(dim=-1, keepdim=True)
        emb2 = emb2 / emb2.norm(dim=-1, keepdim=True)

        sim = torch.matmul(emb1, emb2.T).cpu().numpy()

    cost = 1 - sim
    row_ind, col_ind = linear_sum_assignment(cost)

    matched_pairs = []
    matched1 = set()
    matched2 = set()

    for i, j in zip(row_ind, col_ind):
        if sim[i][j] >= threshold:
            matched_pairs.append((figs1[i], figs2[j]))
            matched1.add(i)
            matched2.add(j)

    unmatched1 = [figs1[i] for i in range(len(figs1)) if i not in matched1]
    unmatched2 = [figs2[j] for j in range(len(figs2)) if j not in matched2]

    return matched_pairs, unmatched1, unmatched2


def draw_figure_boxes(pdf_path, elements, unmatched_paths, out_path, color):
    doc = fitz.open(pdf_path)
    for el in elements:
        if "Figure" in el.get("Path", []) and "filePaths" in el:
            for fp in el["filePaths"]:
                if any(os.path.basename(fp) in os.path.basename(u) for u in unmatched_paths):
                    if "Bounds" in el and "Page" in el:
                        rect = adobe_to_fitz_bbox(el["Bounds"], doc[el["Page"].__int__()].rect.height)
                        shape = doc[el["Page"]].new_shape()
                        shape.draw_rect(rect)
                        shape.finish(color=color, width=2.0)
                        shape.commit()
    doc.save(out_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    return out_path

def draw_table_boxes(pdf_path, elements, tables, out_path, color):
    doc = fitz.open(pdf_path)
    for tbl in tables:
        bbox = tbl.get("Bounds")
        if bbox and "Page" in tbl:
            page = doc[tbl["Page"]]
            rect = adobe_to_fitz_bbox(bbox, page.rect.height)
            shape = page.new_shape()
            shape.draw_rect(rect)
            shape.finish(color=color, width=1.5)
            shape.commit()
    doc.save(out_path,incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    return out_path

import pandas as pd

def load_excel_tables(table_dir):
    tables = []
    for fname in sorted(os.listdir(table_dir)):
        if fname.endswith(".xlsx"):
            fpath = os.path.join(table_dir, fname)
            try:
                df = pd.read_excel(fpath)
                tables.append({"filename": fname, "dataframe": df})
            except Exception as e:
                print(f"❌ Failed to read {fname}: {e}")
    return tables

from scipy.optimize import linear_sum_assignment
import numpy as np

def match_excel_tables(tables1, tables2, threshold=0.65):
    n, m = len(tables1), len(tables2)
    sim_matrix = np.zeros((n, m))

    def flatten_table(df):
        return " ".join(df.columns.astype(str).tolist() + df.astype(str).values.flatten().tolist())

    for i, t1 in enumerate(tables1):
        t1_flat = flatten_table(t1["dataframe"])
        for j, t2 in enumerate(tables2):
            t2_flat = flatten_table(t2["dataframe"])
            sim_matrix[i, j] = cosine_sim(t1_flat, t2_flat)

    cost_matrix = 1 - sim_matrix
    row_ind, col_ind = linear_sum_assignment(cost_matrix)

    matched = []
    unmatched1 = set(range(n))
    unmatched2 = set(range(m))

    for i, j in zip(row_ind, col_ind):
        if sim_matrix[i, j] >= threshold:
            matched.append((tables1[i], tables2[j]))
            unmatched1.discard(i)
            unmatched2.discard(j)

    unmatched1 = [tables1[i] for i in unmatched1]
    unmatched2 = [tables2[j] for j in unmatched2]

    return matched, unmatched1, unmatched2


def ko_table_cellwise(df1, df2):
    changes = []
    rows = min(len(df1), len(df2))
    for r in range(rows):
        for col in df1.columns:
            t1 = str(df1.iloc[r][col]) if col in df1.columns else ""
            t2 = str(df2.iloc[r][col]) if col in df2.columns else ""
            t1_tokens = simple_tokenize(t1)
            t2_tokens = simple_tokenize(t2)
            delta = ko_compare_tokens(t1_tokens, t2_tokens)
            if delta:
                changes.append((r, col, delta))
    return changes

def highlight_ko_cells_on_pdf(pdf_path, elements, table_diff, out_path, color):
    doc = fitz.open(pdf_path)

    # Convert table_diff to a set of (row, col) for fast lookup
    diff_cells = set((r, c) for r, c, _ in table_diff)

    for el in elements:
        if not ("Path" in el and "Table" in el["Path"]):
            continue
        if "Text" in el and el["Text"].strip() and "Bounds" in el and "Page" in el:
            path = el["Path"]
            row = col = None

            # Try to extract row and column index from path
            for i in range(len(path)):
                if path[i] == "Row" and i + 1 < len(path):
                    try:
                        row = int(path[i + 1])
                    except ValueError:
                        continue
                if path[i] == "Cell" and i + 1 < len(path):
                    try:
                        col = int(path[i + 1])
                    except ValueError:
                        continue

            if row is not None and col is not None:
                # Match against diff_cells
                if (row, col) in diff_cells:
                    bbox = el["Bounds"]
                    page = doc[el["Page"]]
                    rect = adobe_to_fitz_bbox(bbox, page.rect.height)
                    shape = page.new_shape()
                    shape.draw_rect(rect)
                    shape.finish(color=color, width=1.5)
                    shape.commit()

    doc.save(out_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    return out_path





def full_text_comparison(data1, data2, pdf1_path, pdf2_path):
    elems1 = data1["elements"]
    elems2 = data2["elements"]

    import re

    def split_sentences(elements):
      sentences = []
      for el in elements:
          # Skip table text
          if "Path" in el and "Table" in el["Path"]:
              continue
          if "Text" in el and el["Text"].strip():
              raw = el["Text"].strip()

              # Split on line breaks OR periods followed by whitespace+capital letter
              splits = re.split(r'(?:\n+|(?<=[^0-9])\.(?=\s+[A-Z]))', raw)

              for sent in splits:
                  sent = sent.strip()
                  if sent:
                      sentences.append({
                          "text": sent,
                          "source_element": el
                      })
      return sentences


    sents1 = split_sentences(elems1)
    sents2 = split_sentences(elems2)

    import numpy as np
    from scipy.optimize import linear_sum_assignment

    def match_sentences_optimal(sents1, sents2, threshold=0.65):
        n, m = len(sents1), len(sents2)
        sim_matrix = np.zeros((n, m))

        for i in range(n):
            for j in range(m):
                sim_matrix[i, j] = cosine_sim(sents1[i]["text"], sents2[j]["text"])

        # Convert to cost matrix (Hungarian solves minimum cost, so we invert)
        cost_matrix = 1 - sim_matrix
        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        matched = []
        unmatched1 = []
        unmatched2 = set(range(m))

        for i, j in zip(row_ind, col_ind):
            if sim_matrix[i, j] >= threshold:
                matched.append((sents1[i], sents2[j]))
                unmatched2.discard(j)
            else:
                unmatched1.append(sents1[i])

        # Add unmatched sents1 not in row_ind
        unmatched1.extend(sents1[i] for i in range(n) if i not in row_ind)
        unmatched2 = [sents2[j] for j in unmatched2]

        return matched, unmatched1, unmatched2


    matched_pairs, unmatched1, unmatched2 = match_sentences_optimal(sents1, sents2)

    pdf1_annotated = pdf1_path.replace(".pdf", "_annotated.pdf")
    pdf2_annotated = pdf2_path.replace(".pdf", "_annotated.pdf")

    highlight_pdf(
        pdf1_path,
        [s["source_element"] for s in unmatched1],
        [("deleted", 0, [s["text"]]) for s in unmatched1],
        pdf1_annotated,
        color=(1, 0, 0)
    )

    highlight_pdf(
        pdf2_path,
        [s["source_element"] for s in unmatched2],
        [("added", 0, [s["text"]]) for s in unmatched2],
        pdf2_annotated,
        color=(0, 1, 0)
    )


    # === Figures ===
    figs1 = extract_figure_paths(os.path.dirname(pdf1_path))
    figs2 = extract_figure_paths(os.path.dirname(pdf2_path))
    matched_figures, unmatched_figures_1, unmatched_figures_2 = compare_figures(figs1, figs2)

    pdf1_annotated = draw_figure_boxes(pdf1_annotated, elems1, unmatched_figures_1, pdf1_annotated, color=(0, 0, 1))
    pdf2_annotated = draw_figure_boxes(pdf2_annotated, elems2, unmatched_figures_2, pdf2_annotated, color=(1, 1, 0))

    # === Excel Table Matching & KO Cell Comparison ===
    excel1 = load_excel_tables(os.path.join(os.path.dirname(pdf1_path), "tables"))
    excel2 = load_excel_tables(os.path.join(os.path.dirname(pdf2_path), "tables"))

    matched_xlsx, unmatched_excel1, unmatched_excel2 = match_excel_tables(excel1, excel2)

    # Highlight unmatched tables in PDF1
    for ux in unmatched_excel1:
        for el in elems1:
            if "filePaths" in el and any(ux["filename"] in fp for fp in el["filePaths"]):
                pdf1_annotated = draw_table_boxes(pdf1_annotated, elems1, [el], pdf1_annotated, color=(0, 0, 0))

    # Highlight unmatched tables in PDF2
    for ux in unmatched_excel2:
        for el in elems2:
            if "filePaths" in el and any(ux["filename"] in fp for fp in el["filePaths"]):
                pdf2_annotated = draw_table_boxes(pdf2_annotated, elems2, [el], pdf2_annotated, color=(0.6, 0.3, 0.1))

    # Highlight KO cell diffs for matched tables
    for t1, t2 in matched_xlsx:
        diffs = ko_table_cellwise(t1["dataframe"], t2["dataframe"])
        if diffs:
            el1 = next((el for el in elems1 if "filePaths" in el and any(t1["filename"] in fp for fp in el["filePaths"])), None)
            el2 = next((el for el in elems2 if "filePaths" in el and any(t2["filename"] in fp for fp in el["filePaths"])), None)
            if el1:
                pdf1_annotated = highlight_ko_cells_on_pdf(pdf1_annotated, elems1, diffs, pdf1_annotated, color=(0.4, 0.2, 0.1))

            if el2:
                pdf2_annotated = highlight_ko_cells_on_pdf(pdf2_annotated, elems2, diffs, pdf2_annotated, color=(0.4, 0.2, 0.1))



    return (
        pdf1_annotated, pdf2_annotated,
        matched_pairs, unmatched1, unmatched2,
        matched_figures, unmatched_figures_1, unmatched_figures_2,
        matched_xlsx, unmatched_excel1, unmatched_excel2
    )



if client_id and client_secret and pdf1 and pdf2:
    if st.button("🚀 Compare PDFs"):
        st.info("Extracting structured content from both PDFs...")
        data1, dir1 = extract_pdf_json(pdf1, client_id, client_secret)
        data2, dir2 = extract_pdf_json(pdf2, client_id, client_secret)

        pdf1_path = os.path.join(dir1, "input1.pdf")
        pdf2_path = os.path.join(dir2, "input2.pdf")
        with open(pdf1_path, "wb") as f1:
            f1.write(pdf1.getvalue())
        with open(pdf2_path, "wb") as f2:
            f2.write(pdf2.getvalue())

        st.session_state["pdf1_path"] = pdf1_path
        st.session_state["pdf2_path"] = pdf2_path

        st.info("Performing KO semantic comparison and annotation...")
        results = full_text_comparison(data1, data2, pdf1_path, pdf2_path)
        st.session_state["comparison_results"] = results

    # 🔄 Optional Reset
    if st.button("🔄 Reset Comparison"):
        st.session_state.pop("comparison_results", None)
        st.experimental_rerun()

    # ✅ Display results if available
    if "comparison_results" in st.session_state:
        (
            pdf1_annotated, pdf2_annotated,
            matched_pairs, unmatched1, unmatched2,
            matched_figures, unmatched_figures_1, unmatched_figures_2,
            matched_xlsx, unmatched_excel1, unmatched_excel2
        ) = st.session_state["comparison_results"]


        st.success("✅ Comparison complete! Download annotated PDFs below:")

        with open(pdf1_annotated, "rb") as f1:
            st.download_button("⬇️ Download Annotated PDF 1", f1.read(), file_name="PDF1_annotated.pdf")
        with open(pdf2_annotated, "rb") as f2:
            st.download_button("⬇️ Download Annotated PDF 2", f2.read(), file_name="PDF2_annotated.pdf")

        st.markdown("## 🧾 Summary of Comparison")

        # 📌 SENTENCES
        st.markdown("### 📌 Sentences")

        with st.expander(f"✅ Matched Sentences — {len(matched_pairs)}"):
            for i, (s1, s2) in enumerate(matched_pairs):
                st.markdown(f"**{i+1}. PDF1:** {s1['text']}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;**PDF2:** {s2['text']}")

        with st.expander(f"🟥 Unmatched in PDF1 (Deleted) — {len(unmatched1)}"):
            for i, s in enumerate(unmatched1):
                st.markdown(f"**{i+1}.** {s['text']}")

        with st.expander(f"🟩 Unmatched in PDF2 (Added) — {len(unmatched2)}"):
            for i, s in enumerate(unmatched2):
                st.markdown(f"**{i+1}.** {s['text']}")

        # 📷 FIGURES
        st.markdown("### 📷 Figures")

        matched_figs_count = 0
        if "pdf1_path" in st.session_state and "pdf2_path" in st.session_state:
            figs1_all = extract_figure_paths(os.path.dirname(st.session_state["pdf1_path"]))
            figs2_all = extract_figure_paths(os.path.dirname(st.session_state["pdf2_path"]))
            matched_figs_count = max(len(figs1_all) + len(figs2_all) - len(unmatched_figures_1) - len(unmatched_figures_2), 0)


        with st.expander(f"✅ Matched Figures — {len(matched_figures)}"):
            st.markdown("_Note: Matched figures are shown side-by-side._")

            for i, (fig1, fig2) in enumerate(matched_figures):
                st.markdown(f"**Matched Pair {i+1}:**")
                col1, col2 = st.columns(2)
                with col1:
                    st.image(fig1, caption="PDF1", use_column_width=True)
                with col2:
                    st.image(fig2, caption="PDF2", use_column_width=True)



        with st.expander(f"🟥 Unmatched in PDF1 — {len(unmatched_figures_1)}"):
            for i, fig in enumerate(unmatched_figures_1):
                col1, col2 = st.columns([1, 5])
                with col1:
                    st.image(fig, width=100)
                with col2:
                    st.markdown(f"**{i+1}.** {os.path.basename(fig)}")

        with st.expander(f"🟩 Unmatched in PDF2 — {len(unmatched_figures_2)}"):
            for i, fig in enumerate(unmatched_figures_2):
                col1, col2 = st.columns([1, 5])
                with col1:
                    st.image(fig, width=100)
                with col2:
                    st.markdown(f"**{i+1}.** {os.path.basename(fig)}")

        # 📊 TABLES
        st.markdown("### 📊 Tables")

        with st.expander(f"✅ Matched Tables — {len(matched_xlsx)}"):
            for i, (t1, t2) in enumerate(matched_xlsx):
                st.markdown(f"**{i+1}.** {t1['filename']} ⟷ {t2['filename']}")
                col1, col2 = st.columns(2)
                with col1:
                    st.caption("📄 PDF1 Table")
                    st.dataframe(t1["dataframe"])
                with col2:
                    st.caption("📄 PDF2 Table")
                    st.dataframe(t2["dataframe"])

        with st.expander(f"🟥 Unmatched in PDF1 — {len(unmatched_excel1)}"):
            for i, t in enumerate(unmatched_excel1):
                st.markdown(f"**{i+1}.** {t['filename']}")
                st.dataframe(t["dataframe"])

        with st.expander(f"🟩 Unmatched in PDF2 — {len(unmatched_excel2)}"):
            for i, t in enumerate(unmatched_excel2):
                st.markdown(f"**{i+1}.** {t['filename']}")
                st.dataframe(t["dataframe"])
