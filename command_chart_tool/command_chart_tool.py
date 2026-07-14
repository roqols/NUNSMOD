"""
command_chart_tool.py
----------------------
Ferramenta para extrair e reimportar texto de arquivos "CommandChartData.xfbin"
do Naruto Ultimate Ninja Storm (tabela de comandos/movelist por personagem).

DIFERENCA IMPORTANTE EM RELACAO AO nuns_text_tool_fixed.py:
  O nuns_text_tool_fixed.py foi feito para o formato de DIALOGO (tabela de
  offsets start/len/end, com BASE_OFFSET=4, modos A/B etc). Isso e um
  sub-formato diferente de nuccChunkBinary, usado em message.bin.xfbin,
  itemtext.bin.xfbin etc.

  CommandChartData.xfbin usa outro sub-formato de nuccChunkBinary, bem
  mais simples: uma sequencia de campos, cada um sendo OU
    - um numero (4 bytes, big-endian uint32), OU
    - um texto: 4 bytes (BE uint32) com o tamanho, seguido dos bytes do
      texto em UTF-8 (sem terminador nulo, sem padding).
  Nao ha tabela de offsets separada: o texto e a estrutura estao juntos,
  na ordem em que aparecem no arquivo. O primeiro uint32 do arquivo e
  sempre (tamanho_total_do_arquivo - 4).

  Cada personagem tem seu proprio "chunk" dentro do xfbin (ex: cmd1nrt
  para o Naruto, cmd1ssk para o Sasuke, etc). Por isso essa ferramenta
  trabalha em duas camadas:
    1) abre o container .xfbin (varios chunks nuccChunkBinary, um por
       personagem) usando o modulo xfbin_lib (precisa estar na mesma
       pasta ou no PYTHONPATH -- veja README.md ao lado).
    2) para cada chunk, aplica o parser de campos numero/texto acima.

DEPENDENCIA:
  Precisa da pasta "xfbin_lib_vendor" (incluida junto) no mesmo diretorio
  deste script, ou apontada via variavel de ambiente XFBIN_LIB_PATH.
  Essa pasta e uma copia do xfbin_lib (SutandoTsukai181 / mosamadeeb),
  MIT license, que sabe ler/escrever o container .xfbin em si.

AVISO:
  O reempacotamento do container (comando "rebuild") usa a escrita do
  xfbin_lib, que reproduz o arquivo quase byte-a-byte, exceto por 1 byte
  de "flag/versao" por chunk que a biblioteca nao preserva (confirmado
  comparando um roundtrip sem nenhuma alteracao com o arquivo original:
  36720 bytes, 38 bytes diferentes, todos indo de 24 ou 73 para 0). Isso
  e uma limitacao conhecida da biblioteca (usada e testada pela comunidade
  de modding), nao um bug desta ferramenta. Teste sempre o resultado no
  jogo antes de distribuir uma traducao.

MODOS DE USO (linha de comando):
  list      <arquivo.xfbin>
  segment   <arquivo.xfbin> <nome_do_chunk>
  extract   <arquivo.xfbin> <saida.json>
  selftest  <arquivo.xfbin>
  rebuild   <arquivo.xfbin> <traducoes.json> <saida.xfbin>
"""

import sys
import os
import json
import struct

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_default_lib = os.path.join(SCRIPT_DIR, "xfbin_lib_vendor")
_lib_path = os.environ.get("XFBIN_LIB_PATH", _default_lib)
sys.path.insert(0, _lib_path)
sys.path.insert(0, os.path.join(_lib_path, "xfbin", "util", "binary_reader"))

try:
    from xfbin.xfbin_reader import read_xfbin
    from xfbin.xfbin_writer import write_xfbin
except ImportError as e:
    sys.stderr.write(
        "Nao consegui importar xfbin_lib. Verifique se a pasta "
        "'xfbin_lib_vendor' esta ao lado deste script (ou defina "
        "XFBIN_LIB_PATH).\nErro original: %s\n" % e
    )
    raise

MAX_STR_LEN = 400  # tamanho maximo plausivel de uma string de comando/movimento


# ---------------------------------------------------------------------------
# Parser do conteudo de CADA chunk (formato numero/texto intercalado)
# ---------------------------------------------------------------------------

def read_u32(data, pos):
    return int.from_bytes(data[pos:pos + 4], "big")


def write_u32(val):
    return val.to_bytes(4, "big")


def _looks_like_text(raw):
    if len(raw) == 0:
        return False
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c.isprintable() for c in s)


def parse_chunk_fields(data):
    """
    Analisa o conteudo de um chunk (bytes de um cmd1xxx.bin) e devolve uma
    lista de campos na ordem em que aparecem:
      {"kind": "text", "pos": <offset do campo>, "value": <str>}
      {"kind": "num",  "pos": <offset do campo>, "value": <int>}
    'pos' e o offset do proprio campo de 4 bytes (tamanho, no caso de texto;
    ou o numero, no caso de numero) dentro de 'data'.
    """
    fields = []
    n = len(data)
    pos = 4  # os primeiros 4 bytes sao o header (tamanho_total - 4)
    header_val = read_u32(data, 0)

    while pos + 4 <= n:
        val = read_u32(data, pos)
        if 0 < val <= MAX_STR_LEN and pos + 4 + val <= n:
            raw = data[pos + 4:pos + 4 + val]
            if _looks_like_text(raw):
                fields.append({"kind": "text", "pos": pos, "value": raw.decode("utf-8")})
                pos += 4 + val
                continue
        fields.append({"kind": "num", "pos": pos, "value": val})
        pos += 4

    return header_val, fields


def rebuild_chunk_bytes(data, fields, new_texts):
    """
    Reconstroi os bytes de um chunk a partir dos campos originais, trocando
    o texto de cada campo "text" pelo valor em new_texts (dict: indice do
    campo dentro da lista 'fields' -> nova string). Campos "num" e campos
    "text" nao mencionados em new_texts permanecem inalterados.
    """
    out = bytearray()
    for i, f in enumerate(fields):
        if f["kind"] == "text":
            text = new_texts.get(i, f["value"])
            encoded = text.encode("utf-8")
            out += write_u32(len(encoded))
            out += encoded
        else:
            out += write_u32(f["value"])

    header = write_u32(len(out))
    return bytes(header) + bytes(out)


def selftest_chunk(data):
    header_val, fields = parse_chunk_fields(data)
    new_texts = {i: f["value"] for i, f in enumerate(fields) if f["kind"] == "text"}
    rebuilt = rebuild_chunk_bytes(data, fields, new_texts)
    return rebuilt == data, fields


# ---------------------------------------------------------------------------
# Camada do container .xfbin (varios chunks, um por personagem)
# ---------------------------------------------------------------------------

def _iter_binary_chunks(xfbin_obj):
    """Gera (page, chunk) para cada chunk do tipo nuccChunkBinary no xfbin."""
    for page in xfbin_obj.pages:
        for chunk in page.chunks:
            if type(chunk).__name__ == "NuccChunkBinary":
                yield page, chunk


def cmd_list(xfbin_path):
    xfbin_obj = read_xfbin(xfbin_path)
    for page, chunk in _iter_binary_chunks(xfbin_obj):
        data = chunk.data
        print(f"{chunk.name:20s} path={chunk.filePath:30s} bytes={len(data)}")


def cmd_segment(xfbin_path, chunk_name):
    xfbin_obj = read_xfbin(xfbin_path)
    for page, chunk in _iter_binary_chunks(xfbin_obj):
        if chunk.name == chunk_name:
            header_val, fields = parse_chunk_fields(chunk.data)
            print(f"chunk={chunk.name}  header(len-4)={header_val}  "
                  f"bytes_reais={len(chunk.data)}")
            for i, f in enumerate(fields):
                if f["kind"] == "text":
                    print(f"  [{i:3d}] TEXT @0x{f['pos']:04X}  {f['value']!r}")
                else:
                    print(f"  [{i:3d}] NUM  @0x{f['pos']:04X}  {f['value']}")
            return
    print(f"Chunk '{chunk_name}' nao encontrado. Use 'list' para ver os nomes.")


def cmd_extract(xfbin_path, out_json):
    xfbin_obj = read_xfbin(xfbin_path)
    result = {}
    for page, chunk in _iter_binary_chunks(xfbin_obj):
        header_val, fields = parse_chunk_fields(chunk.data)
        result[chunk.name] = [
            {"index": i, "kind": f["kind"], "value": f["value"]}
            for i, f in enumerate(fields)
        ]
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    n_chunks = len(result)
    n_texts = sum(1 for v in result.values() for e in v if e["kind"] == "text")
    print(f"Extraidos {n_chunks} personagens / {n_texts} textos -> {out_json}")


def cmd_selftest(xfbin_path):
    xfbin_obj = read_xfbin(xfbin_path)
    all_ok = True
    for page, chunk in _iter_binary_chunks(xfbin_obj):
        ok, fields = selftest_chunk(chunk.data)
        status = "OK" if ok else "FALHOU"
        if not ok:
            all_ok = False
        print(f"{chunk.name:20s} {status}  ({len(fields)} campos)")
    print("\nResultado geral:", "TUDO OK" if all_ok else "TEM CHUNK COM PROBLEMA")


def cmd_rebuild(xfbin_path, translations_path, out_path):
    with open(translations_path, "r", encoding="utf-8") as f:
        translations = json.load(f)

    xfbin_obj = read_xfbin(xfbin_path)
    changed = 0
    for page, chunk in _iter_binary_chunks(xfbin_obj):
        if chunk.name not in translations:
            continue
        header_val, fields = parse_chunk_fields(chunk.data)

        entry_list = translations[chunk.name]
        new_texts = {}
        for entry in entry_list:
            if entry.get("kind") == "text":
                new_texts[entry["index"]] = entry["value"]

        chunk.data = bytearray(rebuild_chunk_bytes(chunk.data, fields, new_texts))
        changed += 1

    out_bytes = write_xfbin(xfbin_obj)
    with open(out_path, "wb") as f:
        f.write(out_bytes)

    print(f"{changed} chunk(s) atualizado(s). Arquivo salvo em: {out_path}")
    print("AVISO: reveja o aviso no topo do script sobre o byte de "
          "flag/versao por chunk que a biblioteca de container nao preserva.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "list":
        cmd_list(sys.argv[2])
    elif mode == "segment":
        cmd_segment(sys.argv[2], sys.argv[3])
    elif mode == "extract":
        cmd_extract(sys.argv[2], sys.argv[3])
    elif mode == "selftest":
        cmd_selftest(sys.argv[2])
    elif mode == "rebuild":
        cmd_rebuild(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
