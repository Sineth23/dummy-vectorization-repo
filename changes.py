import logging
import streamlit as st
import deeplake
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from langchain_openai.chat_models import ChatOpenAI

# ————— Configure logging —————
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ————— Streamlit layout —————
st.set_page_config(page_title="File-Path Explorer", layout="wide")
st.title("🔍 File-Path Explorer")
st.write(
    """
    Enter the exact **repo-relative** path of a file  
    (e.g. `organization/migrations/0056_merge_20240109_1259.py`)  
    and click **Load Chunks**.
    """
)

# ————— Sidebar config —————
with st.sidebar.form("cfg"):
    st.write("### Configuration")
    ds_path    = st.text_input(
        "Deep Lake dataset path",
        value="C:\\Users\\Sineth\\RecordManager\\dockertest"
    )
    file_path  = st.text_input(
        "File path to load",
        placeholder="organization/migrations/0056_merge_20240109_1259.py"
    )
    do_llm     = st.checkbox("LLM analyze these chunks?", value=False)
    openai_key = st.text_input(
        "OpenAI API key",
        type="password",
        help="Only needed if you check LLM analyze"
    )
    submitted  = st.form_submit_button("Load Chunks")

if not submitted:
    st.stop()

if not ds_path or not file_path:
    st.error("❌ Please fill in both dataset path and file path.")
    st.stop()

# ————— Load Deep Lake (cached) —————
@st.experimental_singleton
def load_ds(path):
    return deeplake.load(path)

try:
    ds = load_ds(ds_path)
except Exception as e:
    st.error(f"❌ Could not load dataset:\n{e}")
    st.stop()

# ————— Find matching chunks —————
matches = []
for i, sample in enumerate(ds):
    meta = sample["metadata"].data().get("value", {})
    if meta.get("file_path") == file_path:
        text = sample["text"].data()
        matches.append((meta, text))

st.write(f"**Found {len(matches)} chunk(s) for** `{file_path}`")
st.markdown("---")

if len(matches) == 0:
    st.info("No embeddings found for that exact file path.")
    st.stop()

# ————— Optional LLM analysis —————
if do_llm:
    if not openai_key:
        st.error("🔑 Please enter your OpenAI API key to run the LLM.")
    else:
        # build a simple prompt that dumps all chunks
        prompt = PromptTemplate(
            input_variables=["chunks"],
            template="""
You are given all the code chunks for a single file:

{chunks}

Please provide:
1. A one-sentence summary of what this file does.
2. Any notable patterns or potential issues.
"""
        )
        llm = ChatOpenAI(
            model_name="gpt-3.5-turbo",
            openai_api_key=openai_key,
            temperature=0
        )
        chain = LLMChain(llm=llm, prompt=prompt)
        all_code = "\n\n---\n\n".join(
            f"[lines {m['start_line']}–{m['end_line']}]\n{text}"
            for m, text in matches
        )
        with st.spinner("Running LLM…"):
            analysis = chain.run({"chunks": all_code})
        st.subheader("🧠 LLM Analysis")
        st.write(analysis)
        st.markdown("---")

# ————— Show raw chunks —————
for idx, (m, text) in enumerate(matches, start=1):
    st.markdown(
        f"**Chunk {idx}:** lines {m.get('start_line')}–{m.get('end_line')}  "
        f"*(type: {m.get('chunk_type')}, lang: {m.get('language')})*"
    )
    st.code(text, language=m.get("language", ""))
    st.markdown("---")
