# Design decisions

One entry per decision that someone might later ask "why?" about. Newest last.

## D-01 · Python 3.14 and dependencies managed with uv
Python 3.14 is the latest stable release; every dependency publishes 3.14 wheels (chromadb ships abi3 wheels, onnxruntime and scikit-learn ship cp314). Direct dependencies use compatible-release pins (~=), and uv.lock pins everything exactly. If a transitive dependency ever lacks 3.14 wheels, fall back to 3.13 by changing .python-version and requires-python.

uv resolves and locks everything (`uv.lock`), installs Python itself, and is fast in CI. The pack's pinned `requirements.txt` (langchain 0.1.0, chromadb 0.3.21, openai 1.0.0, …) is old and very likely conflicts; it is replaced here. A `requirements.txt` is exported from the lock for anyone not using uv. Record this in the Stage 5 revision log.

## D-02 · Embeddings via Chroma's built-in all-MiniLM-L6-v2 (ONNX)
The brief prescribes all-MiniLM-L6-v2. Chroma's default embedding function *is* that model, run through ONNX, so we avoid installing sentence-transformers and PyTorch (hundreds of MB, slow CI). The model downloads once (~80 MB) to the local cache on first use. The same function embeds tickets for the classifier. Revisit if retrieval quality needs a different model.

## D-03 · Intent classification without the language model
Embeddings + logistic regression with calibrated probabilities (FR-08). Model-reported confidence is poorly calibrated; the governance condition needs stated confidence within 5 points of observed accuracy, and routing must be deterministic. This is also free and fast.

## D-04 · Disclosure line written by code, not the model (FR-06)
A fixed template guarantees it appears and makes the check trivial.
