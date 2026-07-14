"""
nunsmod_tool.py
------------------------------------
Ferramenta para extrair e reimportar texto de arquivos .binary do formato
"mode_select"-like do Naruto Ultimate Ninja Storm 1 (extraidos via
xfbin_parser dos nuccChunkBinary).

MUDANCAS NESTA VERSAO (v2):
  1) BASE_OFFSET=4: os campos start/end da tabela sao o offset REAL menos 4.
     Ou seja, offset_real = campo_da_tabela + 4. Sem isso, a leitura cortava
     todo texto 4 bytes adiantado (bug confirmado comparando reconstrucao
     byte a byte com arquivos originais em ingles).
  2) Modo B (entradas compactadas): uma linha de 16 bytes da tabela de
     conteudo pode descrever DUAS entradas quando a primeira e curta
     (tipico de dialogo: tag + nome do personagem + fala):
       modo A: (flag=0, start, len, end)               -> 1 entrada
       modo B: (len_anterior_implicita, start2, len2, end2) -> 2 entradas
     No modo B, a 1a entrada e implicita: comeca onde a anterior terminou
     e tem tamanho = flag (que deixa de ser 0). A 2a entrada e explicita
     e segue o mesmo padrao do modo A.

FORMATO GERAL (heranca do formato original, ainda valido):
  - Campos numericos da tabela sao BIG-ENDIAN uint32 (heranca do PS3).
  - Preambulo com 1-2 entradas especiais (chave interna tipo "mode_select"
    e um rotulo, geralmente em japones) que NUNCA sao traduzidas.
  - Depois do preambulo, cadeia de entradas de 16 bytes (modo A ou B)
    encadeadas: o fim de uma bate com o inicio da proxima.
  - Os textos ficam concatenados sem separador, na ordem das entradas.

MODOS DE USO (linha de comando):
  segment   <arquivo.binary>
  extract   <arquivo.binary> <saida.json>
  selftest  <arquivo.binary>
  validate  <original.binary> <outro.binary>
  rebuild   <original.binary> <traducoes.json> <saida.binary>
"""

import sys
import json

BASE_OFFSET = 4


def read_u32(data, pos):
    return int.from_bytes(data[pos:pos + 4], "big")


def write_u32(val):
    return val.to_bytes(4, "big")


def find_first_entry(data, search_from=0x10, search_to=0x200):
    """
    Acha a primeira entrada valida (flag=0, start, len, end) no preambulo.
    Essa e a entrada "chave interna" (ex: "mode_select").
    Lembrando: offset real = start + BASE_OFFSET.
    """
    n = len(data)
    for pos in range(search_from, min(search_to, n - 16), 4):
        flag = read_u32(data, pos)
        start = read_u32(data, pos + 4)
        length = read_u32(data, pos + 8)
        end = read_u32(data, pos + 12)
        if flag == 0 and end == start + length and 0 < length < 200:
            real_start = start + BASE_OFFSET
            real_end = end + BASE_OFFSET
            if real_end <= n:
                raw = data[real_start:real_end]
                if len(raw) > 0 and (32 <= raw[0] < 127 or raw[0] >= 0xC0):
                    return pos, real_start, length, real_end
    return None


def find_uniform_quads_start(data, after_pos, search_to=0x400):
    """
    A partir de 'after_pos', acha onde comeca uma cadeia de pelo menos 3
    quads (modo A) uniformes consecutivos e encadeados. Isso serve so
    para achar o INICIO da tabela de conteudo; o parse de fato aceita
    modo A e modo B.
    """
    n = len(data)
    pos = after_pos
    while pos + 48 <= min(search_to, n):
        ok = True
        cur_chain_start = None
        p = pos
        for i in range(3):
            flag = read_u32(data, p)
            start = read_u32(data, p + 4)
            length = read_u32(data, p + 8)
            end = read_u32(data, p + 12)
            if flag != 0 or end != start + length:
                ok = False
                break
            if cur_chain_start is not None and start != cur_chain_start:
                ok = False
                break
            cur_chain_start = end
            p += 16
        if ok:
            return pos
        pos += 4
    return None


def parse_entries(data, quads_start):
    """
    Le a cadeia de entradas de 16 bytes a partir de quads_start, aceitando
    modo A (1 entrada) e modo B (2 entradas compactadas), ate a cadeia
    quebrar ou acabar o arquivo.
    Retorna (entries, table_end_pos).
    """
    entries = []
    pos = quads_start
    n = len(data)
    running = None  # posicao real esperada da proxima entrada

    while pos + 16 <= n:
        f0 = read_u32(data, pos)
        f1 = read_u32(data, pos + 4)
        f2 = read_u32(data, pos + 8)
        f3 = read_u32(data, pos + 12)

        real_start_a = f1 + BASE_OFFSET
        real_end_a = f3 + BASE_OFFSET

        # tenta modo A: flag=0, encadeamento bate, start==running (se ja
        # sabemos onde a cadeia deveria comecar)
        is_mode_a = (f0 == 0 and real_end_a == real_start_a + f2
                     and (running is None or real_start_a == running))

        if is_mode_a:
            entries.append({"table_pos": pos, "mode": "A",
                             "start": real_start_a, "length": f2,
                             "end": real_end_a})
            running = real_end_a
            pos += 16
            continue

        # tenta modo B: f0 = tamanho da entrada implicita anterior
        # (so faz sentido se ja sabemos onde a cadeia deveria comecar)
        if running is not None:
            implicit_len = f0
            implicit_start = running
            implicit_end = running + implicit_len
            next_start = f1 + BASE_OFFSET
            next_end = f3 + BASE_OFFSET
            valid_b = (implicit_len < 200 and next_start == implicit_end
                       and next_end == next_start + f2 and f2 < 2000)
            if valid_b:
                entries.append({"table_pos": pos, "mode": "B-implicit",
                                 "start": implicit_start, "length": implicit_len,
                                 "end": implicit_end})
                entries.append({"table_pos": pos, "mode": "B-explicit",
                                 "start": next_start, "length": f2,
                                 "end": next_end})
                running = next_end
                pos += 16
                continue

        # nao bateu nem modo A nem modo B -> cadeia acabou
        break

    return entries, pos


def extract_text(data, entries):
    for e in entries:
        raw = data[e["start"]:e["end"]]
        try:
            e["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            e["text"] = raw.decode("utf-8", errors="replace")
    return entries


def analyze(data):
    first = find_first_entry(data)
    if first is None:
        raise ValueError("Nao consegui achar a chave interna do arquivo.")
    first_pos, first_start, first_len, first_end = first

    # O preambulo e sempre exatamente 2 linhas de 16 bytes (chave interna +
    # rotulo), entao o conteudo comeca 32 bytes depois da chave interna.
    # (Nao usamos mais find_uniform_quads_start pra achar isso, porque
    # arquivos de dialogo costumam comecar o conteudo ja em modo B, o que
    # quebrava a heuristica de "3 quads modo A seguidos".)
    quads_start = first_pos + 32

    entries, table_end = parse_entries(data, quads_start)
    entries = extract_text(data, entries)

    content_text_start = entries[0]["start"] if entries else None

    return {
        "preamble_table_start": first_pos,
        "preamble_table_end": quads_start,
        "preamble_text_start": first_start,
        "content_text_start": content_text_start,
        "quads_start": quads_start,
        "table_end": table_end,
        "entries": entries,
    }


def cmd_segment(path):
    with open(path, "rb") as f:
        data = f.read()
    info = analyze(data)
    preamble_raw = data[info["preamble_text_start"]:info["content_text_start"]]
    print(f"Preambulo (nao traduzir): {preamble_raw!r}")
    print(f"\nTabela de conteudo comeca em 0x{info['quads_start']:X}")
    print(f"{len(info['entries'])} entradas de conteudo:\n")
    for i, e in enumerate(info["entries"]):
        print(f"[{i}] tbl=0x{e['table_pos']:04X} modo={e['mode']:<10} "
              f"start=0x{e['start']:04X} len={e['length']:4d}  text={e['text']!r}")


def cmd_extract(path, out_path):
    with open(path, "rb") as f:
        data = f.read()
    info = analyze(data)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(info["entries"], f, ensure_ascii=False, indent=2)
    print(f"Extraidas {len(info['entries'])} entradas -> {out_path}")


def rebuild_bytes(data, info, new_texts):
    entries = info["entries"]
    new_blobs = []
    for i, e in enumerate(entries):
        text = new_texts[i] if i in new_texts else e["text"]
        new_blobs.append(text.encode("utf-8"))

    running = info["content_text_start"]
    out = bytearray(data)

    i = 0
    while i < len(entries):
        e = entries[i]
        if e["mode"] == "A":
            blob = new_blobs[i]
            start = running
            length = len(blob)
            end = start + length
            pos = e["table_pos"]
            out[pos:pos + 4] = write_u32(0)
            out[pos + 4:pos + 8] = write_u32(start - BASE_OFFSET)
            out[pos + 8:pos + 12] = write_u32(length)
            out[pos + 12:pos + 16] = write_u32(end - BASE_OFFSET)
            running = end
            i += 1
        else:
            # par implicit+explicit, mesma table_pos, uma linha de 16 bytes
            e_impl = e
            e_expl = entries[i + 1]
            blob_impl = new_blobs[i]
            blob_expl = new_blobs[i + 1]

            impl_start = running
            impl_len = len(blob_impl)
            impl_end = impl_start + impl_len

            expl_start = impl_end
            expl_len = len(blob_expl)
            expl_end = expl_start + expl_len

            pos = e_impl["table_pos"]
            out[pos:pos + 4] = write_u32(impl_len)
            out[pos + 4:pos + 8] = write_u32(expl_start - BASE_OFFSET)
            out[pos + 8:pos + 12] = write_u32(expl_len)
            out[pos + 12:pos + 16] = write_u32(expl_end - BASE_OFFSET)

            running = expl_end
            i += 2

    old_content_start = info["content_text_start"]
    old_table_end = entries[-1]["end"] if entries else old_content_start
    new_text_blob = b"".join(new_blobs)

    new_out = bytes(out[:old_content_start]) + new_text_blob + bytes(out[old_table_end:])

    delta = len(new_text_blob) - (old_table_end - old_content_start)
    old_size_field = read_u32(new_out, 0)
    new_size_field = old_size_field + delta
    new_out = write_u32(new_size_field) + new_out[4:]

    return new_out


def cmd_selftest(path):
    with open(path, "rb") as f:
        data = f.read()
    info = analyze(data)
    new_texts = {i: e["text"] for i, e in enumerate(info["entries"])}
    rebuilt = rebuild_bytes(data, info, new_texts)
    if rebuilt == data:
        print(f"{path}: OK -- reconstrucao identica ao original "
              f"({len(data)} bytes, {len(info['entries'])} entradas)")
        return True
    else:
        n = min(len(rebuilt), len(data))
        diffs = [i for i in range(n) if rebuilt[i] != data[i]]
        print(f"{path}: FALHOU -- {len(diffs)} bytes diferentes "
              f"(reconstruido={len(rebuilt)} original={len(data)})")
        return False


def cmd_validate(original_path, other_path):
    with open(original_path, "rb") as f:
        orig_data = f.read()
    with open(other_path, "rb") as f:
        other_data = f.read()

    orig_info = analyze(orig_data)
    other_info = analyze(other_data)

    if len(orig_info["entries"]) != len(other_info["entries"]):
        print(f"AVISO: numero de entradas diferente! "
              f"original={len(orig_info['entries'])} outro={len(other_info['entries'])}")

    new_texts = {i: e["text"] for i, e in enumerate(other_info["entries"])}
    rebuilt = rebuild_bytes(orig_data, orig_info, new_texts)

    print(f"Tamanho reconstruido: {len(rebuilt)} bytes | "
          f"Tamanho real do outro arquivo: {len(other_data)} bytes")

    if rebuilt == other_data:
        print("\n*** VALIDACAO PERFEITA: reconstrucao bate 100% com o arquivo real! ***")
    else:
        n = min(len(rebuilt), len(other_data))
        diffs = [i for i in range(n) if rebuilt[i] != other_data[i]]
        print(f"\nDiferencas encontradas: {len(diffs)} bytes (de {n} comparados)")
        for i in diffs[:30]:
            print(f"  0x{i:04X}: reconstruido=0x{rebuilt[i]:02X}  real=0x{other_data[i]:02X}")
        if len(rebuilt) != len(other_data):
            print(f"  Tamanhos diferentes: reconstruido={len(rebuilt)} real={len(other_data)}")


def cmd_rebuild(original_path, translations_path, out_path):
    with open(original_path, "rb") as f:
        data = f.read()
    with open(translations_path, "r", encoding="utf-8") as f:
        translations = json.load(f)

    info = analyze(data)
    if isinstance(translations, list):
        # formato gerado pelo "extract": lista de entradas completas,
        # usa a ordem da lista como indice e pega so o campo "text".
        new_texts = {i: entry["text"] for i, entry in enumerate(translations)}
    else:
        # formato dict simples: {"0": "texto", "1": "texto", ...}
        new_texts = {int(k): v for k, v in translations.items()}
    rebuilt = rebuild_bytes(data, info, new_texts)

    with open(out_path, "wb") as f:
        f.write(rebuilt)
    print(f"Arquivo reconstruido salvo em: {out_path} ({len(rebuilt)} bytes)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "segment":
        cmd_segment(sys.argv[2])
    elif mode == "extract":
        cmd_extract(sys.argv[2], sys.argv[3])
    elif mode == "selftest":
        cmd_selftest(sys.argv[2])
    elif mode == "validate":
        cmd_validate(sys.argv[2], sys.argv[3])
    elif mode == "rebuild":
        cmd_rebuild(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
