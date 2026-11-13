# Fluxo e decisões de integração

## Captura

O caminho adotado usa scrcpy externo >= 2.3 com áudio raw e gravação WAV em uma FIFO privada. O playback do scrcpy fica desligado. Um FFmpeg externo lê a FIFO e usa o muxer PulseAudio com o sink explicitamente informado, tablet_mic_bus. O microfone não é enviado inicialmente ao sink padrão do notebook.

As opções --audio-source=mic e --no-video existem desde scrcpy 2.1. A gravação raw/WAV exige 2.3. --no-window é acrescentado somente quando o --help do executável anuncia a opção (desde 2.5). Não é usada uma opção --audio-only.

A alternativa de playback SDL com PULSE_SINK não foi adotada como garantia: o backend SDL3 pode passar um dispositivo explícito a libpulse e ignorar a escolha desse ambiente. O fluxo FIFO evita alterar sink padrão ou volume global para resolver isso.

Referências oficiais:

- [scrcpy 2.1](https://github.com/Genymobile/scrcpy/releases/tag/v2.1)
- [scrcpy 2.3](https://github.com/Genymobile/scrcpy/releases/tag/v2.3)
- [scrcpy 2.5](https://github.com/Genymobile/scrcpy/releases/tag/v2.5)
- [Gravação no scrcpy 5.0.1](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/recording.md)
- [Saída PulseAudio do FFmpeg](https://ffmpeg.org/ffmpeg-devices.html#pulse-1)
- [Backend SDL3 PulseAudio](https://github.com/libsdl-org/SDL/blob/release-3.4.18/src/audio/pulseaudio/SDL_pulseaudio.c)
- [Protocolo PulseAudio no PipeWire](https://docs.pipewire.org/page_module_protocol_pulse.html)

O consumidor FFmpeg é iniciado antes do produtor. A abertura da FIFO pode bloquear dentro desses filhos, sem bloquear o lançamento do segundo processo. A confirmação de início exige que o stream com o PID do FFmpeg apareça no barramento esperado; ainda não comprova amostras de voz úteis.

O buffer inicial proposto é 100 ms. Latência final depende de Android, transporte ADB, scrcpy, FFmpeg, servidor e aplicativo, e precisa de medição real.

## Recursos e falhas

A conexão exige serial explícito e transporte ADB já autorizado. A reconexão não força o transporte compartilhado nem mata o servidor ADB.

A sessão grava estado atomicamente, com permissões privadas, antes de avançar na criação. Limpeza confere identidades de processo e propriedades dos módulos. Um recurso com identidade diferente é preservado. O bloqueio cobre rotas, níveis e ciclo de vida da sessão. Falha de introspecção impede ações de conexão/rota.

A inspeção distingue playback (sink-input) de captura (source-output). Fluxos de módulos com client '-' não bloqueiam sozinhos; clientes numéricos bloqueiam de forma conservadora. Essa política também pode bloquear medidores de nível e não resolve todas as corridas do servidor.

## Navegadores

A central detecta executáveis e versões externos; não cria fork do Chromium, copia perfis nem injeta configurações no navegador. Instalar outro navegador não cria o barramento nem seleciona o microfone de uma chamada já aberta.

A seleção da fonte, permissão de microfone do site e início de uma nova captura continuam no navegador. Firefox ESR é alternativa a avaliar com o aplicativo de chamada escolhido; não foi declarada equivalência de todas as funções ou sites.
