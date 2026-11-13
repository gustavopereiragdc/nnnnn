#!/usr/bin/env python3
"""
Um comando, dois modos:

    python3 clipes.py youtube <URL> [--clips] [--shorts] ...
        Lê o heatmap do YouTube ("momentos mais revisados"). Se o vídeo não
        tiver heatmap, usa os horários mais citados nos comentários. Encaixa
        o corte nas pausas da fala e, se pedido, baixa os clipes e gera o 9:16.

    python3 clipes.py short <entrada.mp4> [saida.mp4]
        Recorta um vídeo já baixado em Short 9:16 (movimento, depois rosto).

    --legenda transcreve a fala (faster-whisper) e queima a legenda no vídeo,
    com poucas palavras por vez e a palavra falada maior, em destaque.

Requisitos: pip install -r requirements.txt  +  ffmpeg no sistema.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yt_dlp


def _garantir_python() -> None:
    """O python3 do sistema traz um yt-dlp antigo, que não baixa o YouTube atual."""
    try:
        partes = tuple(int(p) for p in yt_dlp.version.__version__.split(".")[:3])
    except Exception:
        partes = (0, 0, 0)
    if partes >= (2025, 1, 1):
        return
    raiz = os.path.dirname(os.path.abspath(__file__))
    atual = os.path.abspath(sys.executable)
    for nome in ("venv", ".venv"):
        caminho = os.path.abspath(os.path.join(raiz, nome, "bin", "python"))
        # O binário é o mesmo do sistema; o que muda é o caminho, que ativa o venv.
        if os.path.isfile(caminho) and caminho != atual:
            os.execv(caminho, [caminho, *sys.argv])


_garantir_python()

# ───────────────────────── heatmap / cortes ──────────────────────────────────
CLIP_DURACAO_S = 30
MAX_RESOLUCAO = 720
FRAGMENTOS_PARALELOS = 4
MAX_EXPANSAO_S = 8
SILENCIO_DB = -30
SILENCIO_MIN_S = 0.35
VOZ_HZ = (200, 3000)
MAX_COMENTARIOS = 300       # comentários do topo lidos quando não há heatmap
JANELA_COMENTARIO_S = 20    # timestamps dentro dessa distância contam como o mesmo momento
# pares que aparecem em comentário como proporção de tela, não como horário
_ASPECTOS = {(16, 9), (9, 16), (21, 9), (4, 3)}
_TS_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,3}):([0-5]\d)(?!\d)")

# ───────────────────────── recorte 9:16 ──────────────────────────────────────
ASPECTO_W, ASPECTO_H = 9, 16
SAIDA_W, SAIDA_H = 1080, 1920
ANALISE_LARG = 320
FACE_LARG = 640
FACE_INTERVALO_S = 0.12
DELAY_MOVER_S = 0.3
SUAVIZACAO = 0.22
SUAV_FACE = 0.22          # fração do caminho até o rosto, por quadro
VEL_MAX_FRAC = 0.6
VEL_FACE_FRAC = 0.55      # no máximo 55% da largura por segundo
ZONA_MORTA_FACE = 0.012   # tremor do detector abaixo disso não mexe a câmera
FACE_PERDE_S = 0.5
FACE_SCORE = 0.6
MOTION_MIN = 1.5

WHISPER_MODELO = "medium"
WHISPER_IDIOMA = "pt"
LEGENDA_MAX_PALAVRAS = 3
LEGENDA_MAX_S = 2.2
LEGENDA_FONTE = "Liberation Sans"
LEGENDA_ESCALA = 132      # palavra falada, em % do tamanho das outras


def _runtime_js() -> dict:
    """O yt-dlp só resolve os formatos do YouTube com Node 22+ (ou Deno)."""
    cache = getattr(_runtime_js, "cache", None)
    if cache is not None:
        return cache
    candidatos = [
        shutil.which("node"),
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]
    candidatos += sorted(glob.glob(os.path.expanduser(
        "~/.local/share/cursor-agent/versions/*/node")))
    runtime = {"deno": {}}
    vistos = set()
    for path in candidatos:
        if not path or path in vistos or not os.path.isfile(path):
            continue
        vistos.add(path)
        try:
            saida = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        texto = (saida.stdout or saida.stderr or "").strip().lstrip("v")
        try:
            maior = int(texto.split(".", 1)[0])
        except ValueError:
            continue
        if maior >= 22:
            runtime["node"] = {"path": path}
            break
    _runtime_js.cache = runtime
    return runtime


def _opts_ydl(opts: dict) -> dict:
    junto = {"js_runtimes": _runtime_js()}
    junto.update(opts)
    return junto


def extrair_info(url: str) -> dict:
    """Metadados do vídeo (inclui heatmap). Não baixa mídia."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignore_no_formats_error": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(_opts_ydl(opts)) as ydl:
        return ydl.extract_info(url, download=False)


def fmt_ts(segundos: float) -> str:
    s = int(round(segundos))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def top_trechos(info: dict, n: int) -> list:
    heatmap = info.get("heatmap") or []
    return sorted(heatmap, key=lambda h: h["value"], reverse=True)[:n]


def _segundos_do_texto(texto: str, duracao: float) -> set:
    """Horários escritos no comentário (12:41, 1:02:03), em segundos."""
    achados = set()
    for m in _TS_RE.finditer(texto or ""):
        horas = int(m.group(1)) if m.group(1) else None
        minutos = int(m.group(2))
        segundos = int(m.group(3))
        if horas is None and (minutos, segundos) in _ASPECTOS:
            continue
        if horas is not None and minutos >= 60:
            continue
        total = (horas or 0) * 3600 + minutos * 60 + segundos
        if total < 1:
            continue
        if duracao and total > duracao + 1:
            continue
        achados.add(total)
    return achados


def _mediana(valores: list) -> float:
    s = sorted(valores)
    meio = len(s) // 2
    if len(s) % 2:
        return float(s[meio])
    return (s[meio - 1] + s[meio]) / 2.0


def _agrupar_tempos(tempos: list, janela: float) -> list:
    """Junta marcações próximas. A distância é medida a partir da primeira do grupo."""
    if not tempos:
        return []
    tempos = sorted(tempos)
    grupos = [[tempos[0]]]
    for t in tempos[1:]:
        if t - grupos[-1][0] <= janela:
            grupos[-1].append(t)
        else:
            grupos.append([t])
    return grupos


def trechos_de_timestamps(tempos: list, n: int, dur_alvo: float) -> list:
    """Os n grupos mais citados, no mesmo formato do heatmap."""
    grupos = _agrupar_tempos(tempos, JANELA_COMENTARIO_S)
    grupos.sort(key=lambda g: (-len(g), _mediana(g)))
    separacao = max(15.0, dur_alvo * 0.5)
    escolhidos = []
    for g in grupos:
        meio = _mediana(g)
        if any(abs(meio - e["start_time"]) < separacao for e in escolhidos):
            continue
        escolhidos.append({
            "start_time": meio,
            "end_time": meio,
            "value": float(len(g)),
            "origem": "comentarios",
        })
        if len(escolhidos) >= n:
            break
    return escolhidos


def baixar_comentarios(url: str, limite: int = MAX_COMENTARIOS) -> list:
    """Comentários mais relevantes. Não baixa respostas nem o vídeo."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "ignore_no_formats_error": True,
        "skip_download": True,
        "getcomments": True,
        "extractor_args": {
            "youtube": {
                "max_comments": [str(limite), str(limite), "0", "0"],
                "comment_sort": ["top"],
            }
        },
    }
    with yt_dlp.YoutubeDL(_opts_ydl(opts)) as ydl:
        info = ydl.extract_info(url, download=False)
    return list((info or {}).get("comments") or [])


def trechos_dos_comentarios(url: str, n: int, dur_alvo: float,
                            duracao: float) -> tuple:
    """Devolve (trechos, quantos comentários, quantos timestamps)."""
    comentarios = baixar_comentarios(url)
    tempos = []
    for c in comentarios:
        if c.get("parent") not in (None, "root"):
            continue
        for t in _segundos_do_texto(c.get("text") or "", duracao):
            tempos.append(t)
    return trechos_de_timestamps(tempos, n, dur_alvo), len(comentarios), len(tempos)


def janela(trecho: dict, dur_alvo: float, duracao_video: float) -> tuple:
    """Centra o trecho no ponto médio e expande até ~dur_alvo, dentro do vídeo."""
    meio = (trecho["start_time"] + trecho["end_time"]) / 2
    ini = max(0.0, meio - dur_alvo / 2)
    fim = ini + dur_alvo
    if duracao_video and fim > duracao_video:
        fim = duracao_video
        ini = max(0.0, fim - dur_alvo)
    return ini, fim


def _baixar_com_formato(url: str, opts: dict, formatos: tuple):
    """Tenta cada seletor. O YouTube às vezes não oferece o primeiro."""
    ultimo = None
    for fmt in formatos:
        tentativa = dict(opts)
        tentativa["format"] = fmt
        try:
            with yt_dlp.YoutubeDL(_opts_ydl(tentativa)) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            ultimo = e
            if "Requested format is not available" not in str(e):
                raise
    raise ultimo


def baixar_audio(url: str, tmpdir: str) -> str:
    """Baixa só o áudio e devolve o caminho do arquivo."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
    }
    formatos = ("ba[acodec^=mp4a]/ba/bestaudio/best", "bestaudio/best")
    info = _baixar_com_formato(url, opts, formatos)
    return info["requested_downloads"][0]["filepath"]


def detectar_pausas(audio_path: str, db: float, dur_min: float) -> list:
    """Pausas da fala como (inicio, fim) em segundos, absolutos no vídeo.

    Passa-banda 200–3000 Hz tira som fora da voz; silencedetect marca o silêncio.
    """
    filtro = (
        f"highpass=f={VOZ_HZ[0]},lowpass=f={VOZ_HZ[1]},"
        f"silencedetect=noise={db}dB:d={dur_min}"
    )
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", audio_path,
         "-af", filtro, "-f", "null", "-"],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
    )
    pausas, inicio = [], None
    for linha in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*([-\d.]+)", linha)
        if m:
            inicio = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([-\d.]+)", linha)
        if m and inicio is not None:
            pausas.append((inicio, float(m.group(1))))
            inicio = None
    return pausas


def encaixar_em_pausa(ini: float, fim: float, pausas: list,
                      max_exp: float, dur_video: float) -> tuple:
    """Move início/fim para o centro da pausa mais próxima, até max_exp por lado."""
    centros = [(a + b) / 2 for a, b in pausas]

    cands_i = [c for c in centros if ini - max_exp <= c <= ini]
    novo_ini = max(cands_i) if cands_i else ini

    cands_f = [c for c in centros if fim <= c <= fim + max_exp]
    novo_fim = min(cands_f) if cands_f else fim

    novo_ini = max(0.0, novo_ini)
    if dur_video:
        novo_fim = min(dur_video, novo_fim)
    return novo_ini, novo_fim


def baixar_clipes(url: str, ranges: list, out: str, res: int) -> None:
    """Baixa todos os trechos numa passada, em .mp4."""
    outtmpl = os.path.join(
        out, "%(id)s_%(autonumber)02d_%(section_start)d-%(section_end)d.%(ext)s"
    )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "download_ranges": yt_dlp.utils.download_range_func(None, ranges),
        "force_keyframes_at_cuts": True,
        "concurrent_fragment_downloads": FRAGMENTOS_PARALELOS,
    }
    # h264+aac primeiro: copia direto. Os outros cobrem vídeo que só tem AV1 ou HLS.
    formatos = (
        f"bv*[height<={res}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={res}]+ba/b[height<={res}]/bv*+ba/b",
        "bv*+ba/b",
    )
    _baixar_com_formato(url, opts, formatos)


def _modelo_yunet() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "modelos", "face_detection_yunet_2023mar.onnx")


def _detector_rosto(cv2, larg: int, alt: int):
    """Devolve uma função que lista (centro_x, área) na imagem recebida.

    YuNet pega rosto de frente e de lado. Sem o modelo, cai no Haar frontal.
    """
    modelo = _modelo_yunet()
    if hasattr(cv2, "FaceDetectorYN") and os.path.isfile(modelo):
        det = cv2.FaceDetectorYN.create(
            modelo, "", (larg, alt), FACE_SCORE, 0.3, 5000)

        def detectar(bgr):
            det.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, faces = det.detect(bgr)
            if faces is None:
                return []
            return [(float(f[0] + f[2] / 2.0), float(f[2] * f[3])) for f in faces]

        return detectar

    if not hasattr(cv2, "CascadeClassifier"):
        return None
    caminho = os.path.join(cv2.data.haarcascades,
                           "haarcascade_frontalface_default.xml")
    clf = cv2.CascadeClassifier(caminho)
    if clf.empty():
        return None

    def detectar_haar(bgr):
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        min_lado = max(24, int(gray.shape[0] * 0.08))
        achados = clf.detectMultiScale(
            gray, 1.1, 4, minSize=(min_lado, min_lado))
        return [(float(x + w / 2.0), float(w * h)) for (x, y, w, h) in achados]

    return detectar_haar


def _escolher_rosto(rostos, lock, largura):
    """Segura o mesmo rosto. Sem trava, fica o maior."""
    if not rostos:
        return None
    if lock is not None:
        perto = [r for r in rostos if abs(r[0] - lock) <= largura * 0.2]
        if perto:
            return min(perto, key=lambda r: abs(r[0] - lock))
    return max(rostos, key=lambda r: r[1])


def fazer_short(entrada, saida, *, analise_larg=ANALISE_LARG,
                face_intervalo=FACE_INTERVALO_S, delay_mover=DELAY_MOVER_S,
                suav=SUAVIZACAO, vel_max_frac=VEL_MAX_FRAC,
                usar_face=True, usar_motion=True, crf=20, preset="veryfast"):
    """Gera `saida` (9:16) a partir de `entrada`. Devolve o caminho de saída.

    1) acha rostos (YuNet) e segue o mesmo rosto, sem trocar a cada quadro;
    2) sem rosto, usa o centro de massa do movimento;
    3) a câmera re-mira a cada `delay_mover` segundos e desliza até o rosto.
    Os quadros saem por um pipe para o ffmpeg (uma passada, sem arquivo cru).
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "opencv-python/numpy não instalados "
            "(pip install -r requirements.txt)"
        ) from e

    cap = cv2.VideoCapture(entrada)
    if not cap.isOpened():
        raise RuntimeError(f"não consegui abrir {entrada}")

    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    cw = min(W, int(round(H * ASPECTO_W / ASPECTO_H)))
    aw = analise_larg
    ah = max(1, int(round(H * aw / W)))
    escala = W / float(aw)
    fw = min(W, FACE_LARG)
    fh = max(1, int(round(H * fw / W)))
    escala_face = W / float(fw)
    face_cada = max(1, int(round(fps * face_intervalo)))
    delay_frames = max(1, int(round(fps * delay_mover)))
    vel_max = vel_max_frac * W / fps
    vel_face = VEL_FACE_FRAC * W / fps
    perde_max = max(face_cada, int(round(fps * FACE_PERDE_S)))

    detectar = _detector_rosto(cv2, fw, fh) if usar_face else None
    if usar_face and detectar is None:
        print("  (aviso: detector de rosto indisponível; usando só o movimento)")

    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{SAIDA_W}x{SAIDA_H}", "-r", f"{fps}", "-i", "-",
         "-i", entrada,
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", saida],
        stdin=subprocess.PIPE,
    )

    centro = W / 2.0
    alvo = W / 2.0
    buffer = []
    prev_gray = None
    lock = None
    sem_rosto = perde_max + 1
    idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, (aw, ah))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            motion_cx = None
            if usar_motion and prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                col = diff.sum(axis=0).astype(np.float64)
                if col.sum() / (aw * ah) > MOTION_MIN:
                    xs = np.arange(aw)
                    motion_cx = (xs * col).sum() / col.sum() * escala
            prev_gray = gray

            if detectar is not None and idx % face_cada == 0:
                face_img = cv2.resize(frame, (fw, fh))
                rostos = [(cx * escala_face, area)
                          for cx, area in detectar(face_img)]
                escolhido = _escolher_rosto(rostos, lock, fw)
                if escolhido is not None:
                    lock = escolhido[0]
                    sem_rosto = 0
                else:
                    sem_rosto += face_cada

            seguindo = lock is not None and sem_rosto <= perde_max
            desejado = lock if seguindo else motion_cx
            if desejado is not None:
                buffer.append(desejado)

            if idx % delay_frames == 0 and buffer:
                alvo = float(np.median(buffer))
                buffer = []

            suav_uso = SUAV_FACE if seguindo else suav
            vel = vel_face if seguindo else vel_max
            delta = alvo - centro
            if seguindo and abs(delta) < ZONA_MORTA_FACE * W:
                delta = 0.0
            novo = centro + suav_uso * delta
            novo = max(centro - vel, min(centro + vel, novo))
            centro = min(W - cw / 2.0, max(cw / 2.0, novo))

            x0 = int(round(centro - cw / 2.0))
            recorte = frame[:, x0:x0 + cw]
            saida_frame = cv2.resize(recorte, (SAIDA_W, SAIDA_H))
            ff.stdin.write(saida_frame.tobytes())
            idx += 1
    finally:
        cap.release()
        if ff.stdin:
            ff.stdin.close()
        ff.wait()
    return saida


def _preparar_cuda() -> None:
    """Carrega a libcublas se ela estiver no sistema, antes do Whisper abrir a GPU."""
    import ctypes
    pastas = [
        "/usr/local/lib/ollama/cuda_v12",
        "/usr/local/cuda/lib64",
        "/usr/lib/wsl/lib",
    ]
    for pasta in pastas:
        if not os.path.isfile(os.path.join(pasta, "libcublas.so.12")):
            continue
        for nome in ("libcublasLt.so.12", "libcublas.so.12", "libcudart.so.12"):
            caminho = os.path.join(pasta, nome)
            if os.path.isfile(caminho):
                ctypes.CDLL(caminho, mode=ctypes.RTLD_GLOBAL)
        return


def _dispositivo_whisper() -> tuple:
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _esc_ass(texto: str) -> str:
    return (
        texto.replace("\\", "")
        .replace("{", "")
        .replace("}", "")
        .replace("\n", " ")
        .strip()
    )


def _fecha_frase(texto: str) -> bool:
    t = texto.rstrip("\"'”’)")
    return t.endswith((".", "?", "!", "…"))


def _agrupar_palavras(palavras: list) -> list:
    """Grupos de até 3 palavras (ou até uma pausa/frase) para caber na tela."""
    grupos, atual = [], []
    for palavra in palavras:
        if atual:
            dura = palavra[0] - atual[0][0]
            if (len(atual) >= LEGENDA_MAX_PALAVRAS or dura > LEGENDA_MAX_S
                    or _fecha_frase(atual[-1][2])):
                grupos.append(atual)
                atual = []
        atual.append(palavra)
    if atual:
        grupos.append(atual)
    return grupos


def _linha_legenda(grupo: list, ativo: int) -> str:
    """A palavra falada cresce e fica amarela; as outras ficam no tamanho normal."""
    partes = []
    for i, (_, _, texto) in enumerate(grupo):
        t = _esc_ass(texto)
        if not t:
            continue
        if i == ativo:
            partes.append(
                r"{\1c&H00FFFF&\fscx112\fscy112\t(0,90,\fscx"
                + str(LEGENDA_ESCALA)
                + r"\fscy"
                + str(LEGENDA_ESCALA)
                + r")}"
                + t
                + r"{\r}"
            )
        else:
            partes.append(t)
    return " ".join(partes)


def _ass_ts(segundos: float) -> str:
    segundos = max(0.0, segundos)
    h = int(segundos // 3600)
    m = int((segundos % 3600) // 60)
    s = segundos % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _tamanho_video(video: str) -> tuple:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", video],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("ffprobe não leu o vídeo")
    larg, alt = proc.stdout.strip().split(",")
    return int(larg), int(alt)


def _escrever_ass(caminho: str, palavras: list, larg: int, alt: int) -> None:
    vertical = alt > larg
    fonte = max(28, int(alt * (0.042 if vertical else 0.055)))
    outline = max(3, fonte // 14)
    margem = int(alt * (0.20 if vertical else 0.07))
    eventos = []
    for grupo in _agrupar_palavras(palavras):
        for i, (ini, fim, _) in enumerate(grupo):
            fim_ev = grupo[i + 1][0] if i + 1 < len(grupo) else fim
            if fim_ev <= ini:
                fim_ev = ini + 0.05
            eventos.append((ini, fim_ev, _linha_legenda(grupo, i)))

    linhas = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {larg}",
        f"PlayResY: {alt}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Legenda,{LEGENDA_FONTE},{fonte},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,"
        f"70,70,{margem},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for ini, fim, texto in eventos:
        linhas.append(
            f"Dialogue: 0,{_ass_ts(ini)},{_ass_ts(fim)},Legenda,,0,0,0,,{texto}"
        )
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


def _filtro_ass(caminho: str) -> str:
    p = os.path.abspath(caminho)
    p = (p.replace("\\", "\\\\").replace(":", "\\:")
         .replace("'", "\\'").replace(",", "\\,"))
    return f"ass='{p}'"


def _queimar_legenda(video: str, ass: str, saida: str) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", video, "-vf", _filtro_ass(ass),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "copy", "-movflags", "+faststart", saida],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0 or not os.path.isfile(saida):
        detalhe = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(detalhe[-1][:200] if detalhe else "ffmpeg falhou ao queimar a legenda")


class Transcritor:
    """Whisper com timestamp por palavra. O modelo carrega uma vez por execução."""

    def __init__(self, modelo: str, idioma: str | None):
        self.modelo = modelo
        self.idioma = idioma
        self._model = None
        self.device = "cpu"

    def _carregar(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper não instalado (pip install -r requirements.txt)"
            ) from e
        _preparar_cuda()
        device, compute = _dispositivo_whisper()
        print(f"  carregando Whisper {self.modelo} em {device}...")
        try:
            self._model = WhisperModel(self.modelo, device=device, compute_type=compute)
            self.device = device
        except Exception as e:
            if device == "cpu":
                raise
            print(f"  GPU indisponível ({str(e)[:80]}); usando CPU.")
            self._model = WhisperModel(self.modelo, device="cpu", compute_type="int8")
            self.device = "cpu"
        return self._model

    def palavras(self, video: str) -> list:
        model = self._carregar()
        segments, info = model.transcribe(
            video,
            language=self.idioma,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5 if self.device == "cuda" else 1,
            condition_on_previous_text=False,
        )
        saida = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    texto = (w.word or "").strip()
                    if texto and w.start is not None and w.end is not None:
                        saida.append((float(w.start), float(w.end), texto))
            elif (seg.text or "").strip():
                saida.append((float(seg.start), float(seg.end), seg.text.strip()))
        idioma = getattr(info, "language", None) or self.idioma or "?"
        print(f"  {os.path.basename(video)}: {len(saida)} palavras ({idioma})")
        return saida

    def legendar(self, video: str) -> str | None:
        """Queima a legenda e devolve o mp4 novo. None se não houver fala."""
        palavras = self.palavras(video)
        if not palavras:
            return None
        base, _ = os.path.splitext(video)
        ass = base + ".ass"
        saida = base + "_legenda.mp4"
        larg, alt = _tamanho_video(video)
        _escrever_ass(ass, palavras, larg, alt)
        _queimar_legenda(video, ass, saida)
        return saida


def _legendar_pasta(info: dict, args) -> int:
    vid = info.get("id", "")
    base = sorted(
        f for f in glob.glob(os.path.join(args.out, f"{vid}_*.mp4"))
        if "_legenda" not in os.path.basename(f)
    )
    if args.shorts:
        alvos = [f for f in base if "_9x16" in os.path.basename(f)]
    else:
        alvos = [f for f in base if "_9x16" not in os.path.basename(f)]
    if not alvos:
        print("Nenhum clipe para legendar.")
        return 1
    print(f"\nLegendando {len(alvos)} clipe(s) "
          f"(modelo {args.modelo}, idioma {args.idioma})...")
    idioma = None if args.idioma == "auto" else args.idioma
    tr = Transcritor(args.modelo, idioma)
    erros = 0
    for f in alvos:
        try:
            saida = tr.legendar(f)
        except Exception as e:
            erros += 1
            print("  ERRO", os.path.basename(f), "->", str(e)[:200])
            continue
        if saida is None:
            print("  sem fala:", os.path.basename(f))
        else:
            print("  OK", os.path.basename(saida))
    return 1 if erros else 0


def _gerar_shorts(info: dict, out: str, args) -> int:
    vid = info.get("id", "")
    arquivos = sorted(
        f for f in glob.glob(os.path.join(out, f"{vid}_*.mp4"))
        if "_9x16" not in os.path.basename(f)
        and "_legenda" not in os.path.basename(f)
    )
    print(f"\nGerando {len(arquivos)} Shorts 9:16 (movimento -> rosto)...")
    for f in arquivos:
        saida = os.path.splitext(f)[0] + "_9x16.mp4"
        try:
            fazer_short(
                f, saida, suav=args.suav, delay_mover=args.delay,
                usar_face=not args.sem_face,
                usar_motion=not args.sem_motion,
            )
            print("  OK", os.path.basename(saida))
        except Exception as e:
            print("  ERRO", os.path.basename(f), "->", str(e)[:120])
    return 0


def cmd_youtube(args) -> int:
    args.url = re.sub(r"[\s\\]+", "", args.url)
    info = extrair_info(args.url)
    dur_video = info.get("duration") or 0

    print("Título  :", info.get("title"))
    print("Canal   :", info.get("uploader") or info.get("channel"))
    print("Duração :", dur_video, "s")
    print("Views   :", info.get("view_count"))
    print()

    if not dur_video or not info.get("id"):
        print("Esse endereço não abriu um vídeo. O YouTube devolveu outra página.")
        print("Usa só o ID, numa linha só:")
        print("  python3 clipes.py youtube VIDEO_ID --clips --shorts --legenda")
        return 1

    trechos = top_trechos(info, args.top)
    if not trechos:
        print("Sem heatmap neste vídeo.")
        print(f"Lendo até {MAX_COMENTARIOS} comentários do topo "
              "(a seção inteira não é lida)...")
        try:
            trechos, n_com, n_ts = trechos_dos_comentarios(
                args.url, args.top, args.dur, dur_video)
        except Exception as e:
            print(f"  (comentários indisponíveis: {str(e)[:120]})")
            return 0
        if not trechos:
            print("Sem timestamps utilizáveis nos comentários.")
            return 0
        print(f"  {n_com} comentários, {n_ts} timestamps. "
              "Usando os horários mais citados.\n")

    alvos = [janela(h, args.dur, dur_video) for h in trechos]

    usar_snap = not args.no_snap
    pausas = []
    if usar_snap:
        print("Analisando áudio para achar as pausas da fala...")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio = baixar_audio(args.url, tmp)
                pausas = detectar_pausas(audio, args.sil_db, args.sil_min)
            print(f"  {len(pausas)} pausas detectadas.\n")
        except Exception as e:
            print(f"  (análise de áudio falhou, usando corte exato: {str(e)[:100]})\n")
            usar_snap = False

    print(f"Top {len(trechos)} trechos:")
    ranges = []
    for i, (h, (ai, af)) in enumerate(zip(trechos, alvos), 1):
        if usar_snap and pausas:
            ini, fim = encaixar_em_pausa(ai, af, pausas, args.max_exp, dur_video)
        else:
            ini, fim = ai, af
        ranges.append((ini, fim))
        extra = (fim - ini) - (af - ai)
        nota = f"  (+{extra:.0f}s p/ pausa)" if extra > 0.5 else ""
        if h.get("origem") == "comentarios":
            cabeca = f"{i}. {fmt_ts(h['start_time'])} ({int(h['value'])} menções)"
        else:
            cabeca = f"{i}. pico {fmt_ts(h['start_time'])} (valor {h['value']:.2f})"
        print(f"  {cabeca}  ->  "
              f"clipe {fmt_ts(ini)}-{fmt_ts(fim)} ({fim - ini:.0f}s){nota}")

    if args.clips:
        os.makedirs(args.out, exist_ok=True)
        print(f"\nBaixando {len(ranges)} clipes em ./{args.out}/ (res <= {args.res}p)...")
        try:
            baixar_clipes(args.url, ranges, args.out, args.res)
            print("Clipes baixados.")
        except Exception as e:
            print("ERRO no download:", str(e)[:200])
            return 1

        codigo = 0
        if args.shorts:
            _gerar_shorts(info, args.out, args)
        if args.legenda:
            codigo = _legendar_pasta(info, args)
        print("Concluído.")
        return codigo

    if args.legenda:
        print("\n--legenda precisa dos arquivos baixados. Rode de novo com --clips.")

    return 0


def cmd_short(args) -> int:
    saida = args.saida or (os.path.splitext(args.entrada)[0] + "_9x16.mp4")
    fazer_short(
        args.entrada, saida,
        analise_larg=args.analise_larg, face_intervalo=args.face_intervalo,
        delay_mover=args.delay, suav=args.suav, vel_max_frac=args.vel_max,
        usar_face=not args.sem_face, usar_motion=not args.sem_motion,
    )
    print("Short salvo:", saida)
    if not args.legenda:
        return 0
    print(f"Legendando (modelo {args.modelo}, idioma {args.idioma})...")
    idioma = None if args.idioma == "auto" else args.idioma
    try:
        leg = Transcritor(args.modelo, idioma).legendar(saida)
    except Exception as e:
        print("ERRO na legenda:", str(e)[:200])
        return 1
    if leg is None:
        print("Sem fala detectada; legenda não gerada.")
    else:
        print("Legenda salva:", leg)
    return 0


def _add_shorts_flags(ap, *, com_analise: bool):
    ap.add_argument("--sem-face", action="store_true", help="ignorar rosto")
    ap.add_argument("--sem-motion", action="store_true", help="ignorar movimento")
    ap.add_argument("--suav", type=float, default=SUAVIZACAO,
                    help="suavidade do pan 0-1 (menor = mais suave)")
    ap.add_argument("--delay", type=float, default=DELAY_MOVER_S,
                    help="segundos parado antes de re-mirar o centro")
    if com_analise:
        ap.add_argument("--analise-larg", type=int, default=ANALISE_LARG)
        ap.add_argument("--face-intervalo", type=float, default=FACE_INTERVALO_S)
        ap.add_argument("--vel-max", type=float, default=VEL_MAX_FRAC)


def _add_legenda_flags(ap):
    ap.add_argument("--legenda", action="store_true",
                    help="transcrever a fala e queimar a legenda no vídeo")
    ap.add_argument("--modelo", default=WHISPER_MODELO,
                    help="modelo Whisper: tiny, base, small, medium, large-v3")
    ap.add_argument("--idioma", default=WHISPER_IDIOMA,
                    help="idioma da fala (pt, en, …) ou auto")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Heatmap do YouTube, clipes nas pausas da fala e Short 9:16."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    yt = sub.add_parser("youtube", help="heatmap, cortes e download")
    yt.add_argument("url", help="URL ou ID do vídeo")
    yt.add_argument("--top", type=int, default=5, help="quantos trechos")
    yt.add_argument("--dur", type=float, default=CLIP_DURACAO_S,
                    help="duração alvo de cada clipe, em segundos")
    yt.add_argument("--res", type=int, default=MAX_RESOLUCAO,
                    help="altura máxima do download")
    yt.add_argument("--clips", action="store_true", help="baixar os clipes")
    yt.add_argument("--out", default="clips", help="pasta de saída")
    yt.add_argument("--max-exp", type=float, default=MAX_EXPANSAO_S,
                    help="expansão máxima por lado ao buscar uma pausa")
    yt.add_argument("--sil-db", type=float, default=SILENCIO_DB,
                    help="limiar de silêncio em dB (mais negativo = mais sensível)")
    yt.add_argument("--sil-min", type=float, default=SILENCIO_MIN_S,
                    help="duração mínima de uma pausa, em segundos")
    yt.add_argument("--no-snap", action="store_true",
                    help="não ajustar o corte pela fala")
    yt.add_argument("--shorts", action="store_true",
                    help="gerar a versão 9:16 de cada clipe baixado")
    _add_shorts_flags(yt, com_analise=False)
    _add_legenda_flags(yt)
    yt.set_defaults(func=cmd_youtube)

    sh = sub.add_parser("short", help="recortar um mp4 local em 9:16")
    sh.add_argument("entrada")
    sh.add_argument("saida", nargs="?", help="default: <entrada>_9x16.mp4")
    _add_shorts_flags(sh, com_analise=True)
    _add_legenda_flags(sh)
    sh.set_defaults(func=cmd_short)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
