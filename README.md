# Central de áudio GTK2

Central para escolher microfone e saída no Linux, ajustar volumes, recuperar o perfil anterior e gerenciar uma captura Android própria com scrcpy. A interface segue a [proposta aprovada](reference/central-audio-proposta.svg).

Versão 0.1.0: experimental. A presença de uma fonte ou processo não comprova chegada de voz, latência ou funcionamento de uma chamada.

## Instalar o pacote Debian

O pacote amd64 fica nos **assets de GitHub Releases**, acompanhado dos fontes correspondentes. Ele não é versionado no repositório.

Antes de instalar, confira o plano do APT no computador de destino:

~~~sh
apt -s install ./central-audio_0.1.0-1_amd64.deb
sudo apt install ./central-audio_0.1.0-1_amd64.deb
~~~

O APT resolve dependências pelos repositórios configurados. A central não executa instalações, adiciona repositórios nem baixa executáveis automaticamente. A instalação real no computador principal requer a decisão do usuário.

O pacote é construído para Debian 13 amd64. Outros sistemas precisam de build e validação próprios; a extensão .deb não garante compatibilidade com toda distribuição Debian/Ubuntu.

## Compilar na pasta do projeto

Requer compilador C, make, pkg-config e desenvolvimento GTK2. A compilação usa bibliotecas dinâmicas do sistema:

~~~sh
make
./audio-center
~~~

Para o pacote Debian, com as Build-Depends de debian/control já disponíveis:

~~~sh
dpkg-buildpackage -us -uc -b
~~~

O Makefile compila na pasta do projeto. Não instala dependências ou arquivos do sistema.

## Dependências por função

| Função | Requisitos |
| --- | --- |
| Interface | GTK2/GLib e bibliotecas calculadas por dpkg-shlibdeps no build |
| Rotas, volume e mute | pactl, servidor PulseAudio ou PipeWire-Pulse acessível na sessão, Bash, awk e utilitários padrão do Debian |
| Controle da sessão própria | Python 3, apenas biblioteca padrão |
| Microfone Android | scrcpy externo **>= 2.3**, FFmpeg externo com muxer PulseAudio e transporte ADB já autorizado |
| Chamada no navegador | Chromium do sistema ou outro navegador compatível, por exemplo Firefox ESR |
| Diagnóstico de processo | procps/pgrep, opcional |
| Diagnóstico de arquivos Bluetooth | obexctl/BlueZ OBEX, opcional; nenhum envio implementado |

Chromium, scrcpy, FFmpeg e suas bibliotecas **não são copiados para o repositório nem embutidos no pacote**. Suas atualizações continuam sob o mecanismo de instalação escolhido pelo usuário. Os opcionais são declarados por função no pacote e precisam ser instalados explicitamente para essas funções.

Verifique as versões oferecidas pela distribuição antes de instalar opcionais:

~~~sh
apt-cache policy scrcpy ffmpeg adb chromium
scrcpy --version
ffmpeg -hide_banner -h muxer=pulse
~~~

Se o scrcpy disponível for antigo, consulte as [instruções oficiais para Linux](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/linux.md). A central aceita um caminho explícito para um scrcpy externo fora do PATH. Ela não presume ausência no computador apenas por não encontrá-lo no PATH.

## Usar o tablet como microfone

1. Prepare e autorize o ADB pelo procedimento oficial do Android/scrcpy. O dispositivo deve aparecer no transporte ADB já configurado. A central não faz pareamento, altera opções de segurança ou reinicia o servidor ADB compartilhado.
2. Use Android 11 ou superior. Em Android 11, mantenha a tela desbloqueada ao iniciar; veja a [documentação oficial de áudio](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/audio.md).
3. Informe na central o serial ADB autorizado ou o endereço IP:porta **já conectado**. Se necessário, informe o caminho do scrcpy externo.
4. Clique em **Verificar dependências** e depois **Conectar**, fora de uma captura/chamada ativa.
5. A central cria o barramento e a fonte somente quando faltam, ou reutiliza recursos existentes que correspondam à ligação esperada. Inicia apenas seu próprio pipeline: scrcpy → FIFO WAV privada → FFmpeg → barramento do tablet → Microfone_Tablet.
6. No Chromium/Firefox ou aplicativo de chamada, escolha **Microfone_Tablet**. Inicie uma nova captura e faça um teste humano de voz e latência.

O nome interno usual da fonte é **tablet_mic**; **Microfone_Tablet** é a descrição exibida. Uma fonte monitor de alto-falantes não é escolhida automaticamente como microfone local.

Conectar, desconectar, reconectar e mudar padrões são recusados quando a introspecção encontra clientes de captura. Medidores de áudio também podem acionar esse bloqueio conservador. Repetir a ação não libera nem encerra a captura.

**Reconectar** reinicia apenas a sessão criada pela central, após verificar o bloqueio. **Desconectar** encerra somente processos com PID, instante de criação e grupo próprios; só remove módulos com a identidade dessa sessão. Recursos externos preexistentes são preservados. Não são usados adb kill-server, adb disconnect ou encerramento amplo por nome de processo.

## Volume, eco e reversão

Escolher um rádio, arrastar uma escala ou marcar Silenciar prepara a seleção; os valores só são enviados ao clicar em **Aplicar**. Os controles de dispositivo afetam todos os seus canais. Antes da primeira mudança, o script guarda os volumes brutos por canal e mute, permitindo restaurar inclusive um balanço assimétrico.

O primeiro padrão de entrada/saída fica guardado até a restauração. Arquivos incompletos ou inválidos são preservados e rejeitados. Um lock comum serializa as alterações. Falhas parciais mantêm o que ainda precisa ser restaurado.

Os comandos de padrão não são comandos de mover streams. Políticas do servidor de áudio podem reagir a mudanças de padrão; por isso a central bloqueia trocas quando há captura e não promete preservar uma chamada por configuração universal. Nenhuma troca foi executada na chamada existente durante o desenvolvimento.

A central não detecta eco nem implementa cancelamento de eco. Prefira fones ou reduza os alto-falantes ao usar o microfone do tablet. Amplificação acima de 100% pode distorcer.

Estado privado do usuário: $XDG_STATE_HOME/central-gtk2-audio, ou ~/.local/state/central-gtk2-audio. A FIFO não é um arquivo de gravação persistente. Logs e identidade da sessão ficam nessa pasta privada; não devem ser enviados ao repositório.

## Bluetooth de arquivos

O bloco OBEX é separado dos controles de áudio. Consulta ferramentas e serviços locais; não inicia pareamento nem transferência e não confirma suporte no Android. O teste de envio/recepção de arquivos continua pendente.

## Supervisão e privacidade

Enquanto a janela estiver aberta, a central acompanha seus processos a cada três segundos e registra mudanças em um log privado de eventos. A presença ou ausência de um processo não determina a causa de uma queda; o log de erro da sessão deve ser analisado antes de atribuí-la ao ADB, Wi-Fi ou áudio.

**Pausar captura · privacidade** encerra explicitamente apenas os produtores próprios, mesmo se houver uma chamada usando a fonte. Mantém as fontes e rotas e não desmuta dispositivos. A pausa trava qualquer retomada implícita: depois de encerrar a captura da chamada, desconecte a sessão e use Conectar explicitamente para começar outra.

O mute da chamada no Chromium/ChatGPT não é exposto de forma confiável pelo estado de mute do PulseAudio. Por isso, **o reinício automático da captura fica bloqueado enquanto o mute da chamada for desconhecido**. A versão atual monitora e permite recuperação explícita protegida; ainda não entrega recuperação automática segura durante uma chamada. Essa função exige uma integração confiável com o estado de privacidade da chamada e continua pendente. Não existe reinício em loop ou ação que remova mute por conta própria.

## Testes e limites

~~~sh
python3 -B -m unittest discover -s tests -v
~~~

A suíte usa executáveis simulados, diretórios privados e FIFOs de teste, sem sockets do áudio pessoal. O [registro de validação](docs/VALIDATION.md) distingue testes automatizados, inspeção visual, instalação isolada e testes de hardware pendentes.

O CI avalia os ambientes Debian disponíveis em cada execução. Isso não equivale a suporte universal nem a aprovação para inclusão no Debian sid. A [documentação Debian](docs/DEBIAN.md) lista os passos restantes.

## Licença

GPL-2.0-only. Consulte [COPYING](COPYING) e [licenças e dependências](docs/LICENSES.md). A release deve oferecer o código correspondente ao pacote, incluindo os scripts de build e empacotamento.
