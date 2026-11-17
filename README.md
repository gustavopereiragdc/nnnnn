# Clipes

Pega os trechos mais revistos de um vídeo do YouTube, corta nas pausas da fala e, se você pedir, gera a versão vertical 9:16.

Tudo roda por `clipes.py`.

## Preparar

O `ffmpeg` precisa estar no sistema (`ffmpeg -version`). As bibliotecas Python ficam no ambiente virtual:

```bash
cd /home/gpereira/Desktop/ent./ent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Nas próximas vezes, só `source .venv/bin/activate` nesta pasta.

## Windows

No PowerShell, nesta pasta. O `ffmpeg` precisa estar no PATH (`ffmpeg -version`). Se não estiver:

```powershell
winget install Gyan.FFmpeg
```

Feche e abra o terminal depois do winget. O download do YouTube também precisa de um runtime JavaScript: Node 22 ou mais novo, ou Deno. Os dois se instalam pelo winget (`OpenJS.NodeJS` ou `DenoLand.Deno`).

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Se o PowerShell bloquear o `Activate.ps1`:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Nas próximas vezes, só `.\.venv\Scripts\Activate.ps1` nesta pasta. Sem ativar, o `python` do sistema entra e o YouTube responde que o formato não existe.

O comando inteiro fica numa linha. Baixa em até 1080p, gera o 9:16 e queima a legenda:

```powershell
python clipes.py youtube g26UssW8mjI --clips --shorts --legenda --res 1080
```

Um mp4 que já está no disco:

```powershell
python clipes.py short video.mp4 --legenda
```

A legenda pede a fonte Liberation Sans. Sem ela, o vídeo sai com a fonte que o ffmpeg achar no Windows. A primeira legenda baixa o modelo `medium` (cerca de 1,5 GB). Se a placa não for encontrada, a transcrição roda na CPU e demora mais.

## YouTube: ver os trechos

Não baixa vídeo. Mostra título, canal e os 5 picos do heatmap, já com o corte ajustado para uma pausa.

Se o YouTube não publicar o heatmap, o mesmo comando lê só os 300 comentários do topo, não a seção inteira. Junta horários escritos como `12:41` que caem a menos de 20 segundos uns dos outros e usa os mais citados. Sem horário nenhum, transcreve o áudio (o modelo `medium` na primeira vez) e escolhe janelas de ~30 s com gancho nos 2 primeiros segundos, pico de volume e frase que se entende sozinha:

```bash
python3 clipes.py youtube "https://www.youtube.com/watch?v=VIDEO_ID"
```

Também aceita só o ID: `python3 clipes.py youtube VIDEO_ID`

## YouTube: baixar os clipes

Cinco clipes de cerca de 30 segundos, até 720p, na pasta `clips/`:

```bash
python3 clipes.py youtube "https://www.youtube.com/watch?v=VIDEO_ID" --clips
```

Três clipes de 20 segundos, em 480p, noutra pasta:

```bash
python3 clipes.py youtube "URL" --clips --top 3 --dur 20 --res 480 --out saida
```

Cortar na duração exata, sem procurar pausa na fala:

```bash
python3 clipes.py youtube "URL" --clips --no-snap
```

Pausa mais curta e corte que pode esticar até 12 segundos de cada lado:

```bash
python3 clipes.py youtube "URL" --clips --sil-min 0.25 --sil-db -35 --max-exp 12
```

## YouTube: baixar e gerar o Short 9:16

Cada clipe ganha um arquivo `*_9x16.mp4` ao lado, em 1080×1920. O recorte segue o movimento e, se o OpenCV tiver detector de rosto, puxa para o rosto mais próximo.

```bash
python3 clipes.py youtube "URL" --clips --shorts
```

Câmera mais parada (re-mira a cada 8 segundos, pan mais suave):

```bash
python3 clipes.py youtube "URL" --clips --shorts --delay 8 --suav 0.08
```

Só movimento, sem tentar achar rosto:

```bash
python3 clipes.py youtube "URL" --clips --shorts --sem-face
```

## Legenda e Transcrição (estilo Opus Clip)

Usa o **faster-whisper** rodando direto na sua placa de vídeo (RTX 3050).
O áudio é transcrito com timestamp de cada palavra e queimado no vídeo:
- Blocos curtos (até 3 palavras por vez, centralizado embaixo).
- A palavra falada no momento exato fica destacada em **amarelo** (estilo karaokê do Opus Clip).
- Mantém o vídeo original intacto e gera dois arquivos novos por clipe:
  - `*_legenda.mp4` (vídeo pronto com a legenda embutida)
  - `*.ass` (arquivo de legenda editável caso queira mudar tamanho, cor ou texto)

### 1. Tudo automático: baixar do YouTube + cortar + 9:16 + legenda

Comando completo para pegar os 5 melhores trechos, recortar na vertical e legendar:

```bash
python3 clipes.py youtube "https://www.youtube.com/watch?v=VIDEO_ID" --clips --shorts --legenda
```

### 2. Apenas os 3 melhores trechos de 20s legendados

```bash
python3 clipes.py youtube "URL" --clips --shorts --legenda --top 3 --dur 20
```

### 3. Legendar mantendo o vídeo horizontal (sem virar Short)

```bash
python3 clipes.py youtube "URL" --clips --legenda
```

### 4. Modelo da legenda

O padrão é o `medium`. A primeira vez baixa cerca de 1,5 GB. Para uma passada mais rápida:

```bash
python3 clipes.py youtube "URL" --clips --shorts --legenda --modelo small
```

### 5. Vídeo em outro idioma (ex: inglês ou detecção automática)

```bash
# Vídeo em inglês:
python3 clipes.py youtube "URL" --clips --shorts --legenda --idioma en

# Deixar o Whisper detectar o idioma sozinho:
python3 clipes.py youtube "URL" --clips --shorts --legenda --idioma auto
```

### 6. Legendar um vídeo local que você já tem no PC

Transforma um arquivo local em Short 9:16 e já adiciona a legenda:

```bash
python3 clipes.py short video.mp4 --legenda
```

Ou especificando o nome da saída:

```bash
python3 clipes.py short video.mp4 resultado_9x16.mp4 --legenda
```

## Um mp4 que você já tem

```bash
python3 clipes.py short video.mp4
```

Grava `video_9x16.mp4` na mesma pasta. Para escolher o nome:

```bash
python3 clipes.py short video.mp4 vertical.mp4
```

```bash
python3 clipes.py short video.mp4 --sem-motion --delay 3 --suav 0.2
```

## O que cada flag faz

| Flag | Onde | Efeito |
|---|---|---|
| `--top N` | youtube | quantos trechos (padrão 5) |
| `--dur S` | youtube | duração alvo, em segundos (padrão 30) |
| `--res P` | youtube | altura máxima do download (padrão 720) |
| `--clips` | youtube | baixa os mp4; sem isso, só lista |
| `--out DIR` | youtube | pasta de saída (padrão `clips`) |
| `--shorts` | youtube | gera o 9:16 de cada clipe baixado |
| `--no-snap` | youtube | não empurra o corte até uma pausa |
| `--max-exp S` | youtube | quanto o clipe pode crescer de cada lado atrás de uma pausa (padrão 8) |
| `--sil-db DB` | youtube | limiar de silêncio (padrão -30; mais negativo pega pausa mais baixa) |
| `--sil-min S` | youtube | pausa mínima, em segundos (padrão 0.35) |
| `--delay S` | os dois | segundos com o enquadramento parado antes de re-mirar (padrão 5) |
| `--suav 0-1` | os dois | suavidade do movimento (menor = mais lento) |
| `--sem-face` | os dois | não usa rosto |
| `--sem-motion` | os dois | não usa movimento |
| `--legenda` | os dois | transcreve e queima a legenda |
| `--modelo` | os dois | modelo Whisper (padrão `medium`) |
| `--idioma` | os dois | idioma da fala (padrão `pt`, ou `auto`) |

O detector de rosto usa o OpenCV 4 (`opencv-python>=4.10,<5`). A versão 5 não traz esse módulo.
