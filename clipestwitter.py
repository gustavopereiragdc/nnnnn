#!/usr/bin/env python3
"""
Short vertical a partir de um vídeo do Twitter/X (ou de um mp4 local).

A webcam do streamer fica em cima e a gameplay embaixo, no mesmo corte
dos Shorts de gameplay: a câmera ocupa ~46% da altura e o jogo fica com
o maior pedaço possível do quadro, sem a webcam no meio da cena.

    python3 clipestwitter.py https://x.com/usuario/status/123 --legenda
    python3 clipestwitter.py video.mp4 --webcam 1440,700,420,320
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipes

SAIDA_W, SAIDA_H = 1080, 1920
SPLIT_WEBCAM = 0.46
AMOSTRAS = 8


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "opencv-python/numpy não instalados (pip install -r requirements.txt)"
        ) from e
    return cv2, np


def _detector(cv2, larg, alt):
    modelo = clipes._modelo_yunet()
    if not (hasattr(cv2, "FaceDetectorYN") and os.path.isfile(modelo)):
        return None
    det = cv2.FaceDetectorYN.create(modelo, "", (larg, alt), clipes.FACE_SCORE, 0.3, 5000)

    def detectar(bgr):
        det.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, faces = det.detect(bgr)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]

    return detectar


def _mediana_caixa(caixas):
    xs, ys, ws, hs = zip(*caixas)
    return (
        sorted(xs)[len(xs) // 2],
        sorted(ys)[len(ys) // 2],
        sorted(ws)[len(ws) // 2],
        sorted(hs)[len(hs) // 2],
    )


def _expandir_webcam(box, larg, alt):
    """Do rosto para o quadro da webcam: cabeça, ombros e um pouco de fundo."""
    x, y, w, h = box
    nx = x - 1.05 * w
    ny = y - 0.55 * h
    nw = w * 3.10
    nh = h * 3.40
    nx = max(0.0, nx)
    ny = max(0.0, ny)
    nw = min(nw, larg * 0.46, larg - nx)
    nh = min(nh, alt * 0.50, alt - ny)
    lado = min(larg, alt)
    if nw < lado * 0.16:
        falta = lado * 0.16 - nw
        nx = max(0.0, nx - falta / 2)
        nw = min(larg - nx, nw + falta)
    if nh < lado * 0.16:
        falta = lado * 0.16 - nh
        ny = max(0.0, ny - falta / 2)
        nh = min(alt - ny, nh + falta)
    # A webcam de canto costuma ir até a borda; o rosto fica no meio dela.
    cx, cy = nx + nw / 2.0, ny + nh / 2.0
    extra_x = larg * 0.06
    extra_y = alt * 0.05
    if cx > larg * 0.5:
        nx = max(0.0, nx - extra_x)
        nw = min(larg * 0.50, larg - nx, nw + extra_x)
    else:
        nw = min(larg * 0.50, larg - nx, nw + extra_x)
    if cy > alt * 0.5:
        ny = max(0.0, ny - extra_y)
        nh = min(alt * 0.55, alt - ny, nh + extra_y)
    else:
        nh = min(alt * 0.55, alt - ny, nh + extra_y)
    return int(nx), int(ny), max(2, int(nw)), max(2, int(nh))


# A webcam de stream fica num canto. Cada um é lido sozinho, em tamanho maior.
_CANTOS = (
    ("superior esquerdo", 0.0, 0.0),
    ("superior direito", 1.0, 0.0),
    ("inferior esquerdo", 0.0, 1.0),
    ("inferior direito", 1.0, 1.0),
)


def _cantos(frame, larg, alt):
    cw, ch = int(larg * 0.42), int(alt * 0.48)
    for nome, ax, ay in _CANTOS:
        x = 0 if ax == 0.0 else larg - cw
        y = 0 if ay == 0.0 else alt - ch
        yield nome, x, y, frame[y:y + ch, x:x + cw]


def achar_webcam(video: str):
    """Procura o rosto nos quatro cantos. O canto com mais aparições é a webcam."""
    cv2, _np = _cv()
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"não consegui abrir {video}")
    larg = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    alt = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    detectar = _detector(cv2, 320, 240)
    if detectar is None:
        cap.release()
        raise RuntimeError("detector de rosto indisponível")

    por_canto = {nome: [] for nome, _, _ in _CANTOS}
    area_min = larg * alt * 0.0006
    area_max = larg * alt * 0.20
    for i in range(1, AMOSTRAS + 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(total - 1, int(i * total / (AMOSTRAS + 1))))
        ok, frame = cap.read()
        if not ok:
            continue
        for nome, ox, oy, crop in _cantos(frame, larg, alt):
            ch, cw = crop.shape[:2]
            escala = 1.0
            if cw > 720:
                escala = 720.0 / cw
                crop = cv2.resize(crop, (720, max(1, int(ch * escala))))
            for x, y, w, h in detectar(crop):
                caixa = (ox + x / escala, oy + y / escala, w / escala, h / escala)
                area = caixa[2] * caixa[3]
                if area < area_min or area > area_max:
                    continue
                por_canto[nome].append(caixa)
    cap.release()

    nome, melhor = max(por_canto.items(), key=lambda item: len(item[1]))
    if not melhor:
        raise RuntimeError("não achei a webcam; passa --webcam x,y,largura,altura")
    print(f"  canto: {nome} ({len(melhor)}/{AMOSTRAS} amostras)")
    rosto = _centro_rosto(melhor)
    return _expandir_webcam(_mediana_caixa(melhor), larg, alt), rosto, (larg, alt)


def _centro_rosto(caixas):
    cxs = sorted(x + w / 2.0 for x, _, w, _ in caixas)
    cys = sorted(y + h / 2.0 for _, y, _, h in caixas)
    meio = len(cxs) // 2
    return cxs[meio], cys[meio]


def _janela_centrada(rosto, webcam, larg, alt, aspecto):
    """Janela com o rosto no centro. Encolhe se a borda do quadro não deixar margem."""
    fx, fy = rosto
    _, _, ww, wh = webcam
    nh = max(float(wh), ww / max(aspecto, 0.01))
    nw = nh * aspecto
    meia_w = min(fx, larg - fx, nw / 2.0)
    meia_h = min(fy, alt - fy, nh / 2.0)
    if meia_w / max(aspecto, 0.01) < meia_h:
        meia_h = meia_w / max(aspecto, 0.01)
    else:
        meia_w = meia_h * aspecto
    meia_w = max(8.0, meia_w)
    meia_h = max(8.0, meia_h)
    return int(fx - meia_w), int(fy - meia_h), int(meia_w * 2), int(meia_h * 2)


def retangulo_gameplay(larg, alt, webcam):
    """O maior retângulo do quadro que não pisa na webcam."""
    x, y, w, h = webcam
    candidatos = [
        (0, 0, larg, y),
        (0, y + h, larg, alt - (y + h)),
        (0, 0, x, alt),
        (x + w, 0, larg - (x + w), alt),
    ]
    candidatos = [c for c in candidatos if c[2] >= larg * 0.35 and c[3] >= alt * 0.35]
    if not candidatos:
        return 0, 0, larg, alt
    return max(candidatos, key=lambda c: c[2] * c[3])


def _recorte(frame, caixa):
    x, y, w, h = caixa
    x2, y2 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    x, y = max(0, x), max(0, y)
    pedaco = frame[y:y2, x:x2]
    return frame if pedaco.size == 0 else pedaco


def _cover(cv2, frame, caixa, dw, dh):
    recorte = _recorte(frame, caixa)
    rh, rw = recorte.shape[:2]
    escala = max(dw / rw, dh / rh)
    nw, nh = max(dw, int(round(rw * escala))), max(dh, int(round(rh * escala)))
    grande = cv2.resize(recorte, (nw, nh))
    ox = max(0, (nw - dw) // 2)
    oy = max(0, (nh - dh) // 2)
    return grande[oy:oy + dh, ox:ox + dw]


def _gameplay(cv2, frame, caixa, dw, dh):
    """A cena inteira, o maior que cabe. O que sobra leva o mesmo quadro embaçado."""
    recorte = _recorte(frame, caixa)
    rh, rw = recorte.shape[:2]
    fundo = _cover(cv2, frame, caixa, dw, dh)
    fundo = cv2.GaussianBlur(fundo, (51, 51), 0)
    escala = min(dw / rw, dh / rh)
    nw, nh = max(1, int(round(rw * escala))), max(1, int(round(rh * escala)))
    limpo = cv2.resize(recorte, (nw, nh))
    ox = max(0, (dw - nw) // 2)
    oy = max(0, (dh - nh) // 2)
    fundo[oy:oy + nh, ox:ox + nw] = limpo
    return fundo


def montar(video, saida, webcam, gameplay, rosto,
           crf=clipes.CRF, preset=clipes.PRESET):
    cv2, np = _cv()
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"não consegui abrir {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    topo = int(SAIDA_H * SPLIT_WEBCAM)
    baixo = SAIDA_H - topo
    larg = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    alt = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    janela = _janela_centrada(rosto, webcam, larg, alt, SAIDA_W / topo)
    import subprocess
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{SAIDA_W}x{SAIDA_H}", "-r", f"{fps}", "-i", "-",
         "-i", video,
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", saida],
        stdin=subprocess.PIPE,
    )
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            tela = np.zeros((SAIDA_H, SAIDA_W, 3), dtype=np.uint8)
            tela[:topo] = _cover(cv2, frame, janela, SAIDA_W, topo)
            tela[topo:] = _gameplay(cv2, frame, gameplay, SAIDA_W, baixo)
            ff.stdin.write(tela.tobytes())
    finally:
        cap.release()
        if ff.stdin:
            ff.stdin.close()
        ff.wait()
    if ff.returncode != 0 or not os.path.isfile(saida):
        raise RuntimeError("ffmpeg não gravou o short")
    return saida


def _parse_webcam(texto, larg, alt):
    partes = [int(p.strip()) for p in texto.split(",")]
    if len(partes) != 4:
        raise RuntimeError("usa --webcam x,y,largura,altura")
    x, y, w, h = partes
    x = min(max(0, x), larg - 2)
    y = min(max(0, y), alt - 2)
    w = min(max(2, w), larg - x)
    h = min(max(2, h), alt - y)
    return x, y, w, h


def _baixar(url, pasta, res):
    os.makedirs(pasta, exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(pasta, "fonte.%(ext)s"),
        "merge_output_format": "mp4",
    }
    formatos = (
        f"bv*[height<={res}]+ba/b[height<={res}]/bv*+ba/b",
        "bv*+ba/b",
    )
    info = clipes._baixar_com_formato(url, opts, formatos)
    caminho = (info.get("requested_downloads") or [{}])[-1].get("filepath")
    if not caminho or not os.path.isfile(caminho):
        achados = sorted(glob.glob(os.path.join(pasta, "fonte.*")))
        if not achados:
            raise RuntimeError("o download não gerou o vídeo")
        caminho = achados[-1]
    return info, caminho


def _legendar(video, modelo, idioma, crf, preset):
    if not clipes._tem_audio(video):
        print("Esse vídeo não tem áudio; legenda não gerada.")
        return video
    idioma_tr = None if idioma == "auto" else idioma
    palavras = clipes.Transcritor(modelo, idioma_tr).palavras(video)
    if not palavras:
        print("Sem fala detectada; legenda não gerada.")
        return video
    larg, alt = clipes._tamanho_video(video)
    topo = int(alt * SPLIT_WEBCAM)
    # A fala fica em cima da webcam, encostada no corte, sem cobrir o jogo.
    margem = max(0, alt - topo + 28)
    base, _ = os.path.splitext(video)
    ass = base + ".ass"
    saida = base + "_legenda.mp4"
    clipes._escrever_ass(ass, palavras, larg, alt, margem_v=margem)
    clipes._queimar_legenda(video, ass, saida, crf=crf, preset=preset)
    return saida


def gerar(args) -> str:
    os.makedirs(args.out, exist_ok=True)
    if os.path.isfile(args.url):
        fonte = args.url
        video_id = os.path.splitext(os.path.basename(fonte))[0]
        info = {}
    else:
        print("Baixando o vídeo...")
        info, fonte = _baixar(args.url, args.out, args.res)
        video_id = info.get("id") or "twitter"
    print("Título  :", info.get("title") or os.path.basename(fonte))
    if info.get("duration"):
        print("Duração :", int(info["duration"]), "s")

    if args.webcam:
        larg, alt = clipes._tamanho_video(fonte)
        webcam = _parse_webcam(args.webcam, larg, alt)
        rosto = (webcam[0] + webcam[2] / 2.0, webcam[1] + webcam[3] / 2.0)
    else:
        print("Procurando a webcam...")
        webcam, rosto, (larg, alt) = achar_webcam(fonte)
    gameplay = retangulo_gameplay(larg, alt, webcam)
    print(f"Webcam   : x={webcam[0]} y={webcam[1]} {webcam[2]}x{webcam[3]}")
    print(f"Gameplay : x={gameplay[0]} y={gameplay[1]} {gameplay[2]}x{gameplay[3]}")

    bruto = os.path.join(args.out, f"{video_id}_stacked.mp4")
    print("Montando o 9:16...")
    montar(fonte, bruto, webcam, gameplay, rosto, crf=args.crf, preset=args.preset)
    if not args.legenda:
        return bruto
    print(f"Legendando (modelo {args.modelo})...")
    return _legendar(bruto, args.modelo, args.idioma, args.crf, args.preset)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Short 9:16 com a webcam em cima e a gameplay embaixo."
    )
    ap.add_argument("url", help="URL do Twitter/X ou caminho de um mp4")
    ap.add_argument("--res", type=int, default=1920, help="altura máxima do download")
    clipes._add_qualidade_flags(ap)
    ap.add_argument("--out", default="clips_twitter", help="pasta de saída")
    ap.add_argument("--webcam", default="", help="caixa manual: x,y,largura,altura")
    ap.add_argument("--legenda", action="store_true", help="transcrever e queimar a legenda na webcam")
    ap.add_argument("--modelo", default=clipes.WHISPER_MODELO)
    ap.add_argument("--idioma", default=clipes.WHISPER_IDIOMA)
    args = ap.parse_args()
    try:
        saida = gerar(args)
    except Exception as e:
        print("ERRO:", str(e)[:300])
        return 1
    print("Pronto:", saida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
