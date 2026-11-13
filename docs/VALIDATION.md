# Registro de validação

Este documento descreve o escopo de validação da versão 0.1.0 e deve acompanhar o resultado final da release.

## Automatizado

- Compilação C/GTK2 com flags de aviso.
- Sintaxe Bash e compilação/importação Python.
- Testes de contrato do pactl com executável simulado estrito: argumentos multicanal, mute, rollback parcial, snapshots corrompidos, dispositivos ausentes, concorrência e recusa durante captura.
- Testes do ciclo de vida com ferramentas simuladas, FIFO real e recursos privados, sem captura Android ou servidor de áudio pessoal.
- Inspeção e instalação/remoção do pacote em ambiente Debian isolado, quando o executor/CI permitir.

Resultados numéricos e links de CI ficam nas notas da release; não se deve inferir aprovação a partir desta lista de escopo.

Na preparação local desta versão, 57 testes isolados passaram. Uma janela real GTK2 foi capturada em Xvfb privado com dados simulados; a autenticação própria foi verificada também por recusa de um cliente sem o cookie. Isso não é um teste do áudio do usuário.

## Inspeção visual

A referência é reference/central-audio-proposta.svg, aprovada pelo usuário. A janela mantém alerta de eco, entrada/saída lado a lado, conexão scrcpy, restauração e OBEX separado. Os controles funcionais acrescentam aplicação explícita, serial ADB e verificação de dependências. Estados do mockup não são copiados como se fossem medições.

Uma captura privada com dados simulados serve para avaliar layout e abertura; não demonstra integração com o desktop, servidor ou dispositivo reais.

## Consulta real, somente leitura

Uma consulta autorizada confirmou um cliente Chromium ligado à fonte descrita como Microfone_Tablet e saída nos alto-falantes internos. A fonte usa nome interno diferente da descrição; isso foi corrigido na seleção da central.

Naquele instante não foi observado produtor scrcpy no grafo. A entrada padrão global era um monitor de saída. Nenhuma alteração foi feita para corrigir isso durante a chamada. Portanto, a consulta não valida o caminho completo de voz.

Serviços locais Bluetooth e OBEX foram observados ativos. Isso não comprova transferência ou suporte do Android.

## Pendentes de hardware

- Android real: autorização ADB, disputa de microfone, início e desconexão.
- Confirmação de voz, latência e estabilidade do pipeline FIFO/FFmpeg no dispositivo.
- Escolha efetiva de Microfone_Tablet em uma nova chamada Chromium e em alternativa Firefox.
- Eco acústico e volumes percebidos.
- Pareamento e transferência de arquivos OBEX.
- Integração confiável com mute da chamada para permitir recuperação automática. Na ausência dessa integração, a retomada automática está bloqueada; supervisão e pausa não são prova de recuperação automática implementada.

O .deb e o CI não substituem essas verificações.
