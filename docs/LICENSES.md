# Licenças e atribuições

O código original deste projeto, seus testes e empacotamento usam **GPL-2.0-only**. A licença integral está em COPYING. A referência SVG foi fornecida e aprovada para este projeto.

O pacote contém o executável da central, seus scripts, entrada de menu e documentação. Não contém Chromium, scrcpy, FFmpeg, perfis de navegador ou cópias das bibliotecas do sistema.

## Bibliotecas dinâmicas

A interface usa GTK2 e GLib instalados pelo sistema. GTK2 é LGPL-2-or-later, com partes LGPL-2.1-or-later; GLib é LGPL-2.1-or-later. O link dinâmico mantém essas bibliotecas substituíveis. Consulte os avisos de copyright dos pacotes da distribuição, que também cobrem suas dependências transitivas.

- [GNU: LGPL 2.1 e compatibilidade](https://www.gnu.org/licenses/license-list.en.html#LGPLv2.1)
- [Texto LGPL 2.1, §§3 e 6](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html)
- [Fontes GTK2](https://gitlab.gnome.org/GNOME/gtk/-/tree/gtk-2-24)
- [GLib](https://gitlab.gnome.org/GNOME/glib)

Não foi incorporado código dessas bibliotecas à central.

## Programas externos

scrcpy é Apache-2.0. Chromium tem licença principal BSD e numerosos componentes com avisos próprios. FFmpeg e suas opções de build possuem licenças próprias. Esses programas são invocados como executáveis separados e não são redistribuídos no pacote da central; os pacotes/fontes oficiais continuam fornecendo suas licenças e atualizações.

A escolha de executáveis externos evita incorporar código Apache-2.0 ao executável GPL-2.0-only. Isso não dispensa obrigações de licença caso uma distribuição futura passe a copiar ou combinar esses componentes; tal mudança exige nova revisão.

- [Licença scrcpy 5.0.1](https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE)
- [Licença principal Chromium](https://chromium.googlesource.com/chromium/src/+/main/LICENSE)
- [Licenças FFmpeg](https://ffmpeg.org/legal.html)
- [GNU: programas separados e agregação](https://www.gnu.org/licenses/gpl-faq.en.html#MereAggregation)

## Correspondência entre fontes e pacote

A release distribui os fontes da mesma versão do .deb, incluindo C, Bash, Python, Makefile, testes e debian/. O identificador do commit e o SHA-256 dos assets permitem conferir a correspondência. Um link para a branch mutável, sozinho, não substitui os fontes da versão distribuída.

[GPLv2, §3](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html)
