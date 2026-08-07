import os
from pathlib import Path
from smolagents import tool

_current_agent = "Agent"


def set_agent_context(name: str) -> None:
    global _current_agent
    _current_agent = name


def _rel(path: str) -> str:
    p = path.replace("\\", "/")
    for marker in ("/paper/", "/survey/"):
        if marker in p:
            after = p.split(marker, 1)[1]
            return ("paper/" if marker == "/paper/" else "") + after
    return p


def _log(action: str, path: str) -> None:
    print(f"[{_current_agent:<8}] {action:<10} | {_rel(path)}", flush=True)


def _is_full_reference_index(path: str) -> bool:
    return _rel(path).replace("\\", "/") == "paper/references/index.md"


def _index_path() -> Path:
    return Path(__file__).resolve().parents[2] / "paper" / "references" / "index.md"


def _reference_rows():
    lines = _index_path().read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith("| REF-")]


@tool
def read_file(file_path: str) -> str:
    """Reads a text file and returns its content.

    Args:
        file_path: Absolute or relative path to the file to read.
    """
    _log("read_file", file_path)
    if _is_full_reference_index(file_path):
        return "Use the injected reference excerpt in the task prompt, or call search_references(query, chapter) for bounded lookup. Do not read the full reference index."
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' does not exist."
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


@tool
def write_file(file_path: str, content: str) -> str:
    """Writes content to a text file, creating parent directories if needed.

    Args:
        file_path: Absolute or relative path to the file to write.
        content: The text content to write to the file.
    """
    _log("write_file", file_path)
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully written to '{file_path}' ({len(content)} characters)."


@tool
def search_references(query: str, chapter: str = "") -> str:
    """Search the global reference index and return up to 20 markdown table rows.

    Args:
        query: Keyword to search in title, tags, key use, or paths.
        chapter: Optional chapter filter such as "03 Alignment".
    """
    q = (query or "").lower()
    chapter = (chapter or "").strip()
    max_rows = 20
    rows = []
    for row in _reference_rows():
        columns = [column.strip() for column in row.strip("|").split("|")]
        if len(columns) < 10:
            continue
        haystack = " ".join(columns).lower()
        chapter_matches = not chapter or columns[4] == chapter or chapter in columns[5]
        query_matches = not q or q in haystack
        if chapter_matches and query_matches:
            rows.append(row)
        if len(rows) >= max_rows:
            break
    header = [
        f"# Reference search results",
        f"Query: {query or '(none)'}",
        f"Chapter filter: {chapter or '(none)'}",
        "Rows returned: {count} (max 20)".format(count=len(rows)),
        "",
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To | Paradigm Tags | Key Use | Note Path | Full Text Path |",
        "|---|---|---:|:---:|---|---|---|---|---|---|",
    ]
    return "\n".join(header + rows) + "\n"


@tool
def list_folder(folder_path: str) -> str:
    """Lists all files and subdirectories in a folder.

    Args:
        folder_path: Path to the folder to list.
    """
    _log("list_folder", folder_path)
    if not os.path.isdir(folder_path):
        return f"Error: '{folder_path}' is not a valid directory."
    entries = os.listdir(folder_path)
    return "\n".join(entries) if entries else "(empty folder)"
