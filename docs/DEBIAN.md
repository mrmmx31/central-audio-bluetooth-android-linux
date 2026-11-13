# Empacotamento Debian

A versão 0.1.0 é um pacote de projeto, não um pacote oficial do Debian.

debian/control separa requisitos de execução da interface/rotas de opcionais por função. As versões das bibliotecas ELF são calculadas por dpkg-shlibdeps no ambiente de build, incluindo transições t64; não são derivadas de uma lista copiada de ldd.

Use APT para simular e resolver a instalação no sistema de destino. O aplicativo e o Makefile não instalam dependências, adicionam fontes ou executam atualizações.

## Validação isolada

O CI usa containers oficiais Debian para construir e testar. A instalação deve ocorrer em um container limpo separado do build, usando o .deb produzido e APT, seguida de teste --version e remoção. Os testes de áudio usam fixtures e não acessam sockets do computador do usuário.

O build amd64 de Debian 13 não deve ser anunciado como binário universal. Outras arquiteturas exigem build nativo ou cross-build, dependências disponíveis e validação. Cada execução sid é evidência apenas daquela revisão do ambiente.

## Caminho para possível inclusão no sid

Uma futura candidatura exige revisão humana e o processo Debian apropriado: manutenção identificável, auditoria completa de copyright/fontes, política de dependências, ITP quando cabível, source package, testes limpos com sbuild/autopkgtest, lintian e reprodução do build. Passar o CI do GitHub não equivale a aceitação ou a cobertura integral da Policy.

Referências oficiais:

- [Debian Policy: relações e dependências](https://www.debian.org/doc/debian-policy/ch-binary.html#dependencies)
- [Debian Policy: gerar dependências de bibliotecas](https://www.debian.org/doc/debian-policy/ch-sharedlibs.html#generating-dependencies-on-shared-libraries)
- [dpkg-shlibdeps](https://manpages.debian.org/trixie/dpkg-dev/dpkg-shlibdeps.1.en.html)
- [Debian Mentors](https://mentors.debian.net/intro-maintainers/)
- [sbuild](https://wiki.debian.org/sbuild)
