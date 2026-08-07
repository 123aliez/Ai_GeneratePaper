from pathlib import Path
import re

PAPER_ROOT = Path(__file__).resolve().parents[1] / "paper"
REFERENCES_ROOT = PAPER_ROOT / "references"
INDEX_PATH = REFERENCES_ROOT / "index.md"

CHAPTER_ORDER = [
    "02 Pretraining",
    "03 Alignment",
    "04 Reasoning",
    "05 MoE",
    "06 Multimodal",
    "07 Open-Source Ecosystem",
    "08 Engineering and Deployment",
    "09 Future Directions",
    "10 Introduction",
]

CHAPTER_KEYWORDS = {
    "02 Pretraining": ["scaling law", "compute-optimal", "pretraining", "pre-train", "data curation", "training corpus", "base model", "tokenizer", "long context", "attention mechanism"],
    "03 Alignment": ["rlhf", "preference optimization", "dpo", "supervised fine-tuning", "instruction tuning", "reward model", "constitutional ai", "safety alignment"],
    "04 Reasoning": ["chain-of-thought", "reasoning", "mathematical reasoning", "verifier", "process reward", "tree of thoughts", "rlvr", "tool use", "agentic"],
    "05 MoE": ["mixture-of-experts", "moe", "expert routing", "router", "expert parallelism", "load balancing"],
    "06 Multimodal": ["multimodal", "vision-language", "image-text", "video", "audio", "omni", "visual instruction"],
    "07 Open-Source Ecosystem": ["open-source", "open source", "open-weight", "open weight", "chinese llm", "china", "model release", "open model"],
    "08 Engineering and Deployment": ["serving", "inference", "quantization", "kv cache", "speculative decoding", "distillation", "compression", "parallelism", "deployment"],
    "09 Future Directions": ["trustworthy", "hallucination", "citation", "retrieval-augmented", "rag", "evaluation", "benchmark", "governance", "long-term"],
    "10 Introduction": ["survey", "overview", "comprehensive overview", "large language models: a survey", "survey on large language models"],
}

TAG_MAP = {
    "scaling": ["scaling law", "compute-optimal", "compute optimal", "chinchilla", "kaplan"],
    "data-curation": ["data curation", "dataset", "corpus", "dedup", "filtering"],
    "pretraining": ["pretraining", "pre-train", "base model", "tokenizer"],
    "architecture": ["transformer", "attention", "positional", "state space", "ssm", "mamba", "rwkv"],
    "alignment": ["rlhf", "preference", "dpo", "sft", "instruction tuning", "reward model", "constitutional"],
    "reasoning": ["reasoning", "chain-of-thought", "cot", "math", "verifier", "rlvr"],
    "moe": ["mixture-of-experts", "moe", "expert routing", "router"],
    "multimodal": ["multimodal", "vision-language", "image", "video", "audio", "omni"],
    "open-source": ["open-source", "open source", "open-weight", "open weight", "chinese llm"],
    "engineering": ["serving", "inference", "quantization", "kv cache", "compression", "parallelism", "deployment"],
    "evaluation": ["evaluation", "benchmark", "mmlu", "helm"],
    "rag": ["retrieval", "rag", "citation"],
    "safety": ["safety", "trustworthy", "hallucination", "bias", "governance"],
}

PEER_REVIEWED_VENUES = [
    "nature", "science", "icml", "neurips", "nips", "iclr", "acl", "emnlp", "naacl", "cvpr", "iccv", "eccv",
    "usenix", "sigcomm", "sosp", "osdi", "mlsys", "sc ", "supercomputing", "jmlr", "tmlr", "tois", "tist",
    "tkde", "tpami", "information fusion", "national science review", "kdd", "www", "sigir", "aaai", "ijcai",
]

INVALID_ARXIV_VALUES = {"", "n/a", "na", "none", "null", "-", "unknown"}

REFINED_ENTRY_OVERRIDES = {
    "Better & Faster Large Language Models via Multi-token Prediction": {
        "evidence": "A",
        "related": "08 Engineering and Deployment",
        "tags": "pretraining; engineering",
        "summary": "用于说明预训练目标从单token next-token prediction扩展到多token预测：辅助损失可提升代码生成能力，并通过自推测解码带来推理加速。",
    },
    "BLOOM: A 176B-Parameter Open-Access Multilingual Language Model": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "pretraining; open-source; architecture",
        "summary": "用于说明早期开源大模型的协作式训练范式：BLOOM 以176B decoder-only Transformer和多语言语料推动前沿LLM的开放可复现研究。",
    },
    "Training Compute-Optimal Large Language Models": {
        "evidence": "B",
        "related": "-",
        "tags": "scaling; pretraining",
        "summary": "用于支撑从参数优先扩展转向计算最优扩展的核心论点：固定算力下模型参数和训练token应近似等比例增长。",
    },
    "DataComp-LM: In Search of the Next Generation of Training Sets for Language Models": {
        "evidence": "A",
        "related": "-",
        "tags": "data-curation; pretraining; evaluation",
        "summary": "用于说明预训练数据策展进入可基准化阶段：DCLM通过系统消融证明模型质量过滤是构建高质量训练集的关键步骤。",
    },
    "DeepSeek-V2: A Strong, Economical, and Efficient Mixture-of-Experts Language Model": {
        "evidence": "B",
        "related": "05 MoE; 08 Engineering and Deployment",
        "tags": "pretraining; architecture; moe; engineering",
        "summary": "用于说明预训练架构向稀疏激活和低成本推理协同演进：MLA压缩KV cache，DeepSeekMoE降低训练和服务成本。",
    },
    "DeepSeek-V3 Technical Report": {
        "evidence": "C",
        "related": "05 MoE; 08 Engineering and Deployment",
        "tags": "pretraining; moe; engineering",
        "summary": "用于说明大规模MoE预训练的工程化成熟：671B总参数模型结合无辅助损失负载均衡、MTP、FP8训练和DualPipe降低训练成本。",
    },
    "DeepSeek LLM: Scaling Open-Source Language Models with Longtermism": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "scaling; pretraining; open-source",
        "summary": "用于说明开源模型重新校准scaling laws：DeepSeek LLM强调数据质量、non-embedding FLOPs/token和中英双语语料对最优配比的影响。",
    },
    "Dolma: An Open Corpus of Three Trillion Tokens for Language Model Pretraining Research": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "data-curation; pretraining; open-source",
        "summary": "用于说明开放预训练语料的可复现基础设施：Dolma提供3T token英文语料和配套策展工具，支撑OLMo式透明训练研究。",
    },
    "The RefinedWeb Dataset for Falcon LLM: Outperforming Curated Corpora with Web Data": {
        "evidence": "A",
        "related": "-",
        "tags": "data-curation; pretraining",
        "summary": "用于说明Web语料经大规模过滤和去重后可匹敌精心混合语料，挑战早期对策划数据绝对优势的假设。",
    },
    "The Falcon Series of Open Language Models": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "pretraining; architecture; open-source",
        "summary": "用于说明Falcon系列以RefinedWeb和多组注意力训练7B/40B/180B开放模型，展示Web数据和工程扩展可支撑高性能开源基座。",
    },
    "The FineWeb Datasets: Decanting the Web for the Finest Text Data at Scale": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "data-curation; pretraining; open-source",
        "summary": "用于说明预训练数据清洗从单次过滤转向系统化蒸馏：FineWeb/FineWeb-Edu通过提取、去重和教育内容过滤提升公开Web语料质量。",
    },
    "Gemini 1.5: Unlocking Multimodal Understanding Across Millions of Tokens of Context": {
        "evidence": "C",
        "related": "05 MoE; 06 Multimodal; 08 Engineering and Deployment",
        "tags": "architecture; moe; multimodal; engineering",
        "summary": "用于说明长上下文、多模态和稀疏MoE在前沿模型中合流：Gemini 1.5将上下文扩展到百万级并覆盖文本、视频和音频。",
    },
    "Gemma: Open Models Based on Gemini Research and Technology": {
        "evidence": "B",
        "related": "03 Alignment; 07 Open-Source Ecosystem",
        "tags": "pretraining; alignment; open-source",
        "summary": "用于说明闭源前沿模型技术向轻量开放模型迁移：Gemma以Gemini技术为基础发布2B/7B开放模型并配套对齐和安全处理。",
    },
    "Gemini 2.5: Pushing the Frontier with Advanced Reasoning, Multimodality, Long Context, and Next Generation Agentic Capabilities": {
        "evidence": "C",
        "related": "04 Reasoning; 05 MoE; 06 Multimodal",
        "tags": "reasoning; moe; multimodal; safety",
        "summary": "用于说明前沿模型把深度推理、原生多模态、百万级上下文和agent能力整合为统一系统，而非单一预训练规模扩展。",
    },
    "Gemma 2: Improving Open Language Models at a Practical Size": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem; 08 Engineering and Deployment",
        "tags": "pretraining; architecture; open-source; engineering",
        "summary": "用于说明实用尺寸开放模型的效率路线：Gemma 2结合知识蒸馏和局部/全局注意力，在2B-27B范围提升性能/成本比。",
    },
    "Gemma 3 Technical Report": {
        "evidence": "C",
        "related": "06 Multimodal; 07 Open-Source Ecosystem; 08 Engineering and Deployment",
        "tags": "multimodal; open-source; engineering",
        "summary": "用于说明轻量开放模型向多模态和长上下文扩展：Gemma 3通过局部/全局注意力、SigLIP视觉编码器和蒸馏降低部署成本。",
    },
    "Language Models are Few-Shot Learners": {
        "evidence": "A",
        "related": "04 Reasoning",
        "tags": "scaling; pretraining; reasoning",
        "summary": "用于说明GPT-3式规模扩展带来的in-context learning：175B自回归模型无需梯度更新即可通过上下文示例完成多任务泛化。",
    },
    "GPT-4 Technical Report": {
        "evidence": "C",
        "related": "03 Alignment; 06 Multimodal; 09 Future Directions",
        "tags": "scaling; multimodal; alignment; safety",
        "summary": "用于说明前沿模型从纯文本预训练走向可预测扩展和多模态系统，同时需要以技术报告和安全评估约束未公开细节。",
    },
    "GPT-5 System Card": {
        "evidence": "C",
        "related": "03 Alignment; 04 Reasoning; 09 Future Directions",
        "tags": "reasoning; safety; evaluation",
        "summary": "用于说明系统卡作为前沿模型证据类型：重点不是架构细节，而是统一模型路由、安全补全、幻觉和欺骗评估。",
    },
    "GPT-5.5 System Card": {
        "evidence": "C",
        "related": "03 Alignment; 04 Reasoning; 09 Future Directions",
        "tags": "reasoning; safety; evaluation",
        "summary": "用于说明更强推理模型的部署证据转向能力分级、防护栈、CoT可监控性和高风险领域评估。",
    },
    "GPT-OSS Model Card: gpt-oss-120b & gpt-oss-20b": {
        "evidence": "D",
        "related": "04 Reasoning; 05 MoE; 07 Open-Source Ecosystem",
        "tags": "reasoning; moe; open-source; safety",
        "summary": "用于说明开放权重推理模型把MoE、可变努力推理、工具使用和安全评估结合起来，是开放生态与推理范式交叉案例。",
    },
    "GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints": {
        "evidence": "A",
        "related": "08 Engineering and Deployment",
        "tags": "architecture; engineering",
        "summary": "用于说明注意力结构如何直接影响服务效率：GQA通过共享KV head在质量和推理吞吐之间取得折中。",
    },
    "Jamba: A Hybrid Transformer-Mamba Language Model": {
        "evidence": "C",
        "related": "05 MoE; 08 Engineering and Deployment",
        "tags": "pretraining; architecture; moe; engineering",
        "summary": "用于说明Transformer之外的混合序列架构路线：Jamba交错注意力层、Mamba层和MoE以降低长上下文KV cache和部署成本。",
    },
    "Kimi K2: Open Agentic Intelligence": {
        "evidence": "B",
        "related": "04 Reasoning; 05 MoE; 07 Open-Source Ecosystem",
        "tags": "reasoning; moe; open-source; alignment",
        "summary": "用于说明开放agentic模型的前沿路线：Kimi K2以万亿级MoE、MuonClip训练和强化学习强化工具使用与复杂任务执行。",
    },
    "LLaMA: Open and Efficient Foundation Language Models": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "scaling; pretraining; open-source",
        "summary": "用于说明高效开放基座模型的转折点：LLaMA用公开数据和更多训练token使小参数模型接近或超过更大闭源基座。",
    },
    "Llama 2: Open Foundation and Fine-Tuned Chat Models": {
        "evidence": "B",
        "related": "03 Alignment; 07 Open-Source Ecosystem",
        "tags": "pretraining; alignment; open-source",
        "summary": "用于说明开放基座模型与对话对齐结合：Llama 2在预训练基础上通过SFT、RLHF和安全评估形成可发布chat模型。",
    },
    "The Llama 3 Herd of Models": {
        "evidence": "B",
        "related": "03 Alignment; 06 Multimodal; 07 Open-Source Ecosystem",
        "tags": "pretraining; alignment; multimodal; open-source",
        "summary": "用于说明开放模型继续通过15T级token、405B密集基座和多轮后训练逼近闭源前沿，同时探索视觉和语音能力接入。",
    },
    "LongRoPE: Extending LLM Context Window Beyond 2 Million Tokens": {
        "evidence": "B",
        "related": "08 Engineering and Deployment",
        "tags": "architecture; engineering",
        "summary": "用于说明长上下文扩展可通过位置编码插值和少量微调实现，而不必完全重训基础模型。",
    },
    "Mamba: Linear-Time Sequence Modeling with Selective State Spaces": {
        "evidence": "A",
        "related": "08 Engineering and Deployment",
        "tags": "architecture; engineering",
        "summary": "用于说明选择性状态空间模型挑战Transformer二次复杂度瓶颈，并在长序列建模中提供线性时间替代路线。",
    },
    "Mistral 7B": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem; 08 Engineering and Deployment",
        "tags": "architecture; engineering; open-source",
        "summary": "用于说明小模型高性能路线：Mistral 7B通过GQA和滑动窗口注意力在低参数规模下提升推理效率和基准表现。",
    },
    "OLMo: Accelerating the Science of Language Models": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "pretraining; data-curation; open-source",
        "summary": "用于说明真正开放的预训练研究栈：OLMo同时开放权重、数据、训练代码、中间检查点和评估流程。",
    },
    "OPT: Open Pre-trained Transformer Language Models": {
        "evidence": "B",
        "related": "07 Open-Source Ecosystem",
        "tags": "pretraining; architecture; open-source",
        "summary": "用于说明GPT-3级模型复现和开放训练日志的重要性：OPT把大规模预训练过程向研究社区开放。",
    },
    "PaLM 2 Technical Report": {
        "evidence": "C",
        "related": "04 Reasoning; 08 Engineering and Deployment",
        "tags": "scaling; pretraining; reasoning; engineering",
        "summary": "用于说明后Chinchilla时期的计算最优缩放、数据混合和混合预训练目标如何使更小模型获得更强多语言和推理能力。",
    },
    "PaLM: Scaling Language Modeling with Pathways": {
        "evidence": "B",
        "related": "04 Reasoning; 08 Engineering and Deployment",
        "tags": "scaling; pretraining; reasoning; engineering",
        "summary": "用于说明超大密集Transformer在Pathways系统上的扩展，以及规模在few-shot、代码和部分BIG-bench任务中带来的非线性能力增益。",
    },
    "Textbooks Are All You Need": {
        "evidence": "B",
        "related": "08 Engineering and Deployment",
        "tags": "data-curation; pretraining; scaling; engineering",
        "summary": "用于说明高质量合成/教科书式数据可显著改变小模型能力边界，削弱单纯参数规模对代码生成性能的解释力。",
    },
    "Phi-3 Technical Report: A Highly Capable Language Model Locally on Your Phone": {
        "evidence": "C",
        "related": "06 Multimodal; 08 Engineering and Deployment",
        "tags": "data-curation; pretraining; engineering; multimodal",
        "summary": "用于说明小语言模型通过高质量筛选数据和合成数据达到移动端可部署能力，并扩展到视觉语言场景。",
    },
    "The Pile: An 800GB Dataset of Diverse Text for Language Modeling": {
        "evidence": "B",
        "related": "-",
        "tags": "data-curation; pretraining",
        "summary": "用于说明早期开放预训练数据混合范式：The Pile以22个高质量子集构成多领域英文语料，支撑开源LM训练。",
    },
    "Pythia: A Suite for Analyzing Large Language Models Across Training and Scaling": {
        "evidence": "A",
        "related": "09 Future Directions",
        "tags": "scaling; pretraining; evaluation; safety",
        "summary": "用于说明可控训练动态研究的重要性：Pythia通过同数据顺序、多规模模型和大量中间检查点支持缩放、记忆和偏见分析。",
    },
    "Qwen2.5 Technical Report": {
        "evidence": "C",
        "related": "03 Alignment; 05 MoE; 07 Open-Source Ecosystem",
        "tags": "pretraining; alignment; moe; open-source",
        "summary": "用于说明中文/多语言开放模型的训练扩展：Qwen2.5将预训练数据扩展到18T tokens，并结合多阶段后训练提升通用能力。",
    },
    "Qwen2.5-1M Technical Report": {
        "evidence": "C",
        "related": "08 Engineering and Deployment",
        "tags": "pretraining; architecture; engineering",
        "summary": "用于说明长上下文能力的训练与推理协同扩展：Qwen2.5-1M通过渐进式长上下文预训练、合成数据和DCA/稀疏注意力扩展到1M tokens。",
    },
    "Qwen3 Technical Report": {
        "evidence": "C",
        "related": "04 Reasoning; 05 MoE; 07 Open-Source Ecosystem",
        "tags": "pretraining; reasoning; moe; open-source",
        "summary": "用于说明开放模型把thinking/non-thinking模式、MoE、多语言预训练和强化学习后训练统一到同一模型系列。",
    },
    "Qwen Technical Report": {
        "evidence": "C",
        "related": "03 Alignment; 04 Reasoning; 07 Open-Source Ecosystem",
        "tags": "pretraining; alignment; reasoning; open-source",
        "summary": "用于说明Qwen早期开放模型系列在3T tokens预训练后，通过SFT/RLHF扩展到中文、代码和数学能力。",
    },
    "RWKV: Reinventing RNNs for the Transformer Era": {
        "evidence": "B",
        "related": "08 Engineering and Deployment",
        "tags": "architecture; engineering",
        "summary": "用于说明RNN式线性推理复杂度可以与Transformer并行训练优势结合，形成长序列建模的另一条架构路线。",
    },
    "Scaling Language Models: Methods, Analysis & Insights from Training Gopher": {
        "evidence": "B",
        "related": "09 Future Directions",
        "tags": "scaling; pretraining; safety",
        "summary": "用于说明Gopher阶段的参数规模扩展和任务收益差异：知识密集任务受益更大，而数学、逻辑和安全问题并不随规模自动解决。",
    },
    "Scaling Laws for Neural Language Models": {
        "evidence": "B",
        "related": "-",
        "tags": "scaling; pretraining",
        "summary": "用于说明Kaplan式神经语言模型缩放律：loss随参数、数据和计算呈幂律下降，是后续计算最优缩放讨论的基线。",
    },
    "UL2: Unifying Language Learning Paradigms": {
        "evidence": "B",
        "related": "04 Reasoning",
        "tags": "pretraining; architecture; reasoning",
        "summary": "用于说明预训练目标不必局限于因果LM：UL2以Mixture-of-Denoisers统一span corruption、prefix LM和causal LM。",
    },
    "YaRN: Efficient Context Window Extension of Large Language Models": {
        "evidence": "B",
        "related": "08 Engineering and Deployment",
        "tags": "architecture; engineering",
        "summary": "用于说明RoPE长度外推的高效路线：YaRN用较少token和训练步数扩展上下文窗口，降低长上下文适配成本。",
    },
}


def relative_to_paper(path: Path) -> str:
    return path.relative_to(PAPER_ROOT).as_posix()


def clean_cell(text: str, max_len: int | None = None) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace("|", "\\|")
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip("，,。.;；:： ") + "…"
    return text


def clean_key_use(text: str, fallback: str) -> str:
    text = text or ""
    text = re.sub(r"---+", " ", text)
    text = re.sub(r"^>\s*", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&gt;", "", text)
    text = clean_cell(text, 180)
    if not text or text == "-":
        return f"用于定位 {fallback} 的核心贡献与可复用证据，使用前需打开 Note Path 核验。"
    return text


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    meta: dict[str, str] = {}
    current_key = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"^\w[\w_-]*:", line):
            key, value = line.split(":", 1)
            current_key = key.strip()
            meta[current_key] = value.strip().strip('"')
        elif line.lstrip().startswith("-") and current_key:
            value = line.lstrip()[1:].strip().strip('"')
            existing = meta.get(current_key, "")
            meta[current_key] = (existing + "; " + value).strip("; ")
    return meta


def extract_section(text: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.M)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", text[start:], re.M)
    end = start + next_heading.start() if next_heading else len(text)
    section = text[start:end]
    section = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", section)
    section = re.sub(r"<[^>]+>", "", section)
    section = re.sub(r"^[-*]\s+", "", section, flags=re.M)
    section = re.sub(r"^#+\s+", "", section, flags=re.M)
    return section


def infer_evidence(meta: dict[str, str], title: str, folder: str) -> str:
    venue = (meta.get("venue") or "").lower()
    arxiv = (meta.get("arxiv") or "").strip().lower()
    haystack = f"{title} {folder} {venue}".lower()
    if "model card" in haystack or "blog" in haystack:
        return "D"
    if "system card" in haystack or "technical report" in haystack or "report" in haystack:
        return "C"
    if any(name in venue for name in PEER_REVIEWED_VENUES):
        return "A"
    if arxiv not in INVALID_ARXIV_VALUES:
        return "B"
    return "E"


def infer_tags(title: str, meta: dict[str, str], summary: str) -> str:
    source = f"{title} {meta.get('tags', '')} {summary}".lower()
    tags = [tag for tag, keywords in TAG_MAP.items() if any(keyword in source for keyword in keywords)]
    return "; ".join(tags[:5]) if tags else "general"


def infer_related(primary: str, title: str, meta: dict[str, str], summary: str) -> str:
    source = f"{title} {meta.get('tags', '')} {summary}".lower()
    related = []
    for chapter, keywords in CHAPTER_KEYWORDS.items():
        if chapter == primary:
            continue
        hits = sum(1 for keyword in keywords if keyword in source)
        threshold = 1 if chapter in {"05 MoE", "06 Multimodal"} else 2
        if chapter == "10 Introduction" and primary != "10 Introduction":
            threshold = 3
        if hits >= threshold:
            related.append(chapter)
    return "; ".join(related[:3]) if related else "-"


def collect_entries() -> list[dict[str, str]]:
    entries = []
    for chapter in CHAPTER_ORDER:
        chapter_dir = REFERENCES_ROOT / chapter
        if not chapter_dir.is_dir():
            continue
        paper_dirs = sorted([path for path in chapter_dir.iterdir() if path.is_dir()], key=lambda path: path.name.lower())
        for paper_dir in paper_dirs:
            md_files = sorted(paper_dir.glob("*.md"), key=lambda path: path.name.lower())
            if not md_files:
                continue
            note_candidates = [path for path in md_files if path.name == "笔记.md"]
            note_path = note_candidates[0] if note_candidates else min(md_files, key=lambda path: path.stat().st_size)
            fulltext_candidates = [path for path in md_files if path != note_path]
            fulltext_path = max(fulltext_candidates, key=lambda path: path.stat().st_size) if fulltext_candidates else note_path
            text = note_path.read_text(encoding="utf-8", errors="replace")
            meta = parse_frontmatter(text)
            title = meta.get("title") or paper_dir.name.split(" - ", 1)[-1]
            year = meta.get("year") or "-"
            summary = clean_key_use(extract_section(text, "一句话总结") or extract_section(text, "核心贡献"), title)
            entry = {
                "chapter": chapter,
                "title": title,
                "year": year,
                "evidence": infer_evidence(meta, title, paper_dir.name),
                "related": infer_related(chapter, title, meta, summary),
                "tags": infer_tags(title, meta, summary),
                "summary": summary,
                "note": relative_to_paper(note_path),
                "fulltext": relative_to_paper(fulltext_path),
            }
            entry.update(REFINED_ENTRY_OVERRIDES.get(title, {}))
            entries.append(entry)
    return entries


def render_index(entries: list[dict[str, str]]) -> str:
    lines = [
        "# Global Reference Index",
        "",
        "This index is the global literature map for cross-chapter reuse. It is generated from each paper folder under `paper/references/`, using `笔记.md` when available and the full-text markdown as fallback.",
        "",
        "## How Agents Should Use This Index",
        "",
        "1. Filter rows where `Primary Chapter` matches the current chapter or `Also Relevant To` includes it.",
        "2. Use `Key Use` and `Paradigm Tags` to decide whether a paper supports the current argument.",
        "3. Open `Note Path` for refined evidence before using a claim; open `Full Text Path` only when source details need verification.",
        "4. Record any newly used cross-chapter paper in the current chapter `todo.md` and in `paper/00 Background & Example/cross-chapter-state.md` after finalization.",
        "5. Do not cite from this index alone; citations must be verified against the note or full text.",
        "",
        "## Evidence Grades",
        "",
        "- A: peer-reviewed venue",
        "- B: arXiv/preprint",
        "- C: technical report/system card",
        "- D: blog/model card",
        "- E: third-party or weakly verifiable source",
        "",
        "## All Papers",
        "",
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To | Paradigm Tags | Key Use | Note Path | Full Text Path |",
        "|---|---|---:|:---:|---|---|---|---|---|---|",
    ]
    for index, entry in enumerate(entries, 1):
        lines.append("| " + " | ".join([
            f"REF-{index:04d}",
            clean_cell(entry["title"], 100),
            clean_cell(entry["year"], 12),
            clean_cell(entry["evidence"], 3),
            clean_cell(entry["chapter"], 40),
            clean_cell(entry["related"], 100),
            clean_cell(entry["tags"], 90),
            clean_cell(entry["summary"], 220),
            clean_cell(entry["note"]),
            clean_cell(entry["fulltext"]),
        ]) + " |")
    lines.extend(["", "## By Chapter"])
    for chapter in CHAPTER_ORDER:
        chapter_entries = [(index, entry) for index, entry in enumerate(entries, 1) if entry["chapter"] == chapter]
        if not chapter_entries:
            continue
        lines.extend(["", f"### {chapter}", ""])
        for index, entry in chapter_entries:
            lines.append(f"- REF-{index:04d} — {clean_cell(entry['title'], 120)} — {clean_cell(entry['tags'], 120)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    entries = collect_entries()
    INDEX_PATH.write_text(render_index(entries), encoding="utf-8")
    print(f"wrote {INDEX_PATH} with {len(entries)} entries")


if __name__ == "__main__":
    main()
