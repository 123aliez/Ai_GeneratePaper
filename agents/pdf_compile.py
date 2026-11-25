"""在独立目录编译组装好的 LaTeX 工程，成功后替换交付 PDF。"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def compile_pdf(latex_dir: Path) -> Path:
    log_path = latex_dir / "compile.log"
    source = (latex_dir / "main.tex").read_text(encoding="utf-8")
    requested = re.search(r"^%\s*!TeX\s+program\s*=\s*(pdflatex|xelatex|lualatex)\s*$",
                          source, re.I | re.M)
    if requested:
        engines = [requested[1].lower()]
    elif re.search(r"\\usepackage(?:\[[^]]*\])?\{(?:fontspec|unicode-math|xeCJK)\}", source):
        engines = ["xelatex"]
    else:
        engines = ["pdflatex", "xelatex"]

    with tempfile.TemporaryDirectory(dir=latex_dir.parent) as tmp, log_path.open("w") as log:
        build = Path(tmp)
        # 只复制源文件与资源，旧的 aux/bbl/PDF 不能影响新一轮编译。
        for item in latex_dir.iterdir():
            if item.name == "compile.log" or (item.stem == "main" and item.suffix in
                    {".aux", ".bbl", ".bcf", ".blg", ".log", ".out", ".pdf", ".toc"}):
                continue
            if item.is_dir():
                shutil.copytree(item, build / item.name)
            elif item.is_file():
                shutil.copy2(item, build / item.name)

        def run(args):
            print("$", *args, file=log, flush=True)
            subprocess.run(args, cwd=build, stdin=subprocess.DEVNULL, stdout=log,
                           stderr=subprocess.STDOUT, timeout=180, check=True)

        for engine in engines:
            command = [engine, "-no-shell-escape", "-interaction=nonstopmode",
                       "-halt-on-error", "-file-line-error", "main.tex"]
            try:
                run(command)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                print(exc, file=log, flush=True)
                continue
            if (build / "main.bcf").is_file():
                run(["biber", "main"])
            else:
                aux = (build / "main.aux").read_text(errors="replace")
                if r"\bibdata{" in aux and r"\citation{" in aux:
                    run(["bibtex", "main"])
            run(command)
            run(command)
            pdf = latex_dir / "main.pdf"
            (build / "main.pdf").replace(pdf)
            return pdf
    raise RuntimeError(f"LaTeX 未能生成 PDF，详见 {log_path}")
