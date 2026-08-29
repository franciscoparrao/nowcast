#!/usr/bin/env python3
"""Verifica que la traducción al español no haya alterado ninguna cifra.

Una traducción sirve para auditar sólo si las cifras son las del original. Este
script extrae todos los números de ambos .tex y compara sus multiconjuntos, de
modo que un dígito cambiado, una cifra perdida o un decimal desplazado aparecen
como diferencia en vez de pasar inadvertidos en 50 páginas de prosa.

Qué se ignora, y por qué:
  - la bibliografía (se copia verbatim, no se traduce)
  - el preámbulo y los bloques de aviso propios de la traducción
  - los entornos tikz (coordenadas de dibujo, no resultados)
  - los números pegados a comandos LaTeX (\\parbox{0.92\\textwidth}, [t], etc.)

Uso:  python3 scripts/check_translation_numbers.py
Sale 0 si las cifras coinciden, 1 si no.
"""

import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN = ROOT / "docs/paper/nowcast-ems.tex"
ES = ROOT / "docs/paper/nowcast-ems-ES.tex"


def body(path: Path) -> str:
    """Cuerpo del documento, sin preámbulo, bibliografía ni tikz."""
    text = path.read_text(encoding="utf-8")
    text = text[text.index("\\begin{document}"):]
    if "\\begin{thebibliography}" in text:
        text = text[: text.index("\\begin{thebibliography}")]
    # el recuadro de aviso sólo existe en la traducción
    text = re.sub(r"\\fbox\{\\parbox.*?\n\}\}\n\\end\{center\}", "", text, flags=re.S)
    text = re.sub(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", "", text, flags=re.S)
    return text


def numbers(text: str) -> collections.Counter:
    """Cifras con significado: se excluyen las que son argumento de un comando."""
    # neutraliza opciones/argumentos LaTeX que llevan números de maquetación
    text = re.sub(r"\\[a-zA-Z]+\s*\[[^\]]*\]", " ", text)
    text = re.sub(r"width=[0-9.]+\\linewidth", " ", text)
    text = re.sub(r"\\[a-zA-Z]*(?:sep|skip|width|height|space)[a-zA-Z]*\s*=?\s*[-0-9.]+\s*[a-z]*", " ", text)
    text = re.sub(r"\\\\\[[0-9.]+pt\]", " ", text)
    text = re.sub(r"\\[vh]space\*?\{[^}]*\}", " ", text)
    text = re.sub(r"0\.[0-9]+\\textwidth", " ", text)
    text = re.sub(r"\\multirow\{[0-9]+\}\{[^}]*\}", " ", text)
    text = re.sub(r"\\multicolumn\{[0-9]+\}", " ", text)
    # 13\,880 y 1\,000 son un solo número
    text = text.replace("\\,", "")
    found = re.findall(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?", text)
    return collections.Counter(found)


def main() -> int:
    en, es = numbers(body(EN)), numbers(body(ES))
    only_en, only_es = en - es, es - en

    if not only_en and not only_es:
        total = sum(en.values())
        print(f"OK — las {total} cifras del cuerpo coinciden entre el original y la traducción.")
        return 0

    print("DIFERENCIAS DE CIFRAS entre el original y la traducción\n")
    if only_en:
        print("  sólo en el ORIGINAL (¿perdidas al traducir?):")
        for v, n in sorted(only_en.items(), key=lambda kv: -kv[1]):
            print(f"    {v}  ×{n}")
    if only_es:
        print("  sólo en la TRADUCCIÓN (¿introducidas o alteradas?):")
        for v, n in sorted(only_es.items(), key=lambda kv: -kv[1]):
            print(f"    {v}  ×{n}")
    print("\nRevisar cada una: una cifra que difiere invalida la auditoría.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
