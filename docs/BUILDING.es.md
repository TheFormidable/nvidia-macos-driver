# Compilar el driver desde el código fuente

`./build.sh` compila el driver completo en un Mac con un solo comando: examina el Mac, instala lo que falta (después
de preguntar), descarga cada fuente externa en una versión fijada y verificada, compila cada componente en orden y
puede preparar un paquete instalable. Nunca usa `sudo` y nunca instala el driver: al final imprime el comando para
hacerlo. La guía completa, en inglés, es [BUILDING.md](BUILDING.md); aquí están el inicio rápido, la referencia de
comandos y opciones y la solución de problemas.

> **Escrito y probado sin un Mac.** `./build.sh` se ha ejecutado en un solo Mac (Intel, macOS 26.7, Xcode 26.5), y solo
> en parte: funcionaron `doctor`, `env`, `--dry-run` y `fetch`, y compiló el plugin, la biblioteca de video (y el RM por
> el camino) y las herramientas pequeñas; los kexts se detuvieron en su comprobación de GNU patch y Python 3.10, que ese
> Mac no tenía (la comprobación ahora pide solo lo que usan los kexts). Ninguno de los cambios hechos en `build.sh`
> después de esa ejecución se ha ejecutado aún en un Mac: la comprobación de herramientas por componente, la elección
> de Homebrew y de las herramientas que toma del `PATH`, la organización del directorio de trabajo, la lista de
> compilación del RM, la configuración de git de `fetch`, la parada sin Xcode (también con una carpeta de desarrollo que ya no está), el `patch` que reciben los scripts (con
> `-f`) y el `PATH` que recibe brew solo se probaron contra los sustitutos.
> Está escrito para el `/bin/bash` 3.2 de macOS y sus herramientas BSD, y se probó en Linux contra
> sustitutos de las herramientas de macOS, Xcode, Homebrew, rustup, los remotos de git y cada script de compilación
> (`tools/test_build_sh.py`). En un sistema que no es macOS solo funcionan `doctor`, `fetch`, `test`, `env` y
> `--dry-run`. `deps` no ha instalado nada real, y `build.sh` todavía no ha compilado NVK ni el traductor ni ha preparado
> ningún paquete.
> Para la primera ejecución esto significa: es probable que algún paso se detenga por algo que ningún sustituto imitó
> (un detalle de Xcode o de Homebrew, el nombre de una fórmula, un aviso del compilador). Ejecuta primero `./build.sh
> doctor` y después `./build.sh`; si un paso se detiene, el mensaje dice cuál es y da un archivo de informe para
> adjuntar al reportar el fallo (ver [Cuando falla la compilación](#cuando-falla-la-compilación)). Un paso que falla
> nunca queda marcado como terminado, así que, una vez corregido, la siguiente ejecución sigue desde ese paso. Los
> scripts de los componentes sí se ejecutaron a mano en el Mac Intel del propietario (macOS 26.7, Xcode 26.5) para los
> kexts, el RM y el plugin; lo que eso mostró sobre un nombre de cuenta corto ya está resuelto (fila `rm` de la tabla de
> problemas).

## Requisitos

- Un Mac con macOS 15.6 o posterior, Intel o Apple silicon. Xcode 26 no funciona en sistemas anteriores, así que
  `deps`, `build`, `package` y `all` los rechazan al empezar. Apple silicon sirve para compilar; todo se compila para
  x86_64, la arquitectura en la que funciona el driver. Un Mac Intel compila el RM y NVKMS de NVIDIA con exactamente
  los mismos comandos que un Mac con Apple silicon: los Makefiles de NVIDIA preguntan al compilador del Mac qué
  opciones de protección solo para x86 acepta, y `build/nv_compile_lines.py` quita lo que eso añade.
- **Xcode 26 o posterior**, el Xcode completo, seleccionado con `xcode-select`. Las herramientas de línea de comandos
  solas no bastan: el plugin usa cabeceras de Metal 4 que solo tiene el SDK de macOS 26. No hace falta ningún Kernel
  Debug Kit. En un Mac sin Xcode ni herramientas de línea de comandos, todos los comandos salvo `help` se detienen al
  empezar con las instrucciones (`doctor` nombra Xcode como lo único que falta): el `git`, `make`, `python3` y `xcrun`
  de macOS son entonces sustitutos que ofrecen instalar las herramientas de línea de comandos, y no se ejecuta ninguno.
  Lo mismo ocurre si la carpeta de desarrollo que indican `DEVELOPER_DIR` o `xcode-select -p` no existe o no contiene
  las herramientas de Apple (un Xcode borrado o renombrado después de seleccionarlo): todas fallarían.
- Un clon git de este repositorio (no un archivo descargado). La ruta del clon, la del directorio de trabajo y la de tu
  carpeta personal no pueden contener espacios, y la del directorio de trabajo tampoco `:` `;` `'` `"` `\` `$` ni una
  comilla invertida.
- Red para `fetch`, para `deps` y para cada compilación de NVK.
- Las herramientas de compilación de NVK, el cargador de Vulkan y el traductor: de dónde salen lo dice
  `NM_TOOLS_FROM`. En un
  **Mac Intel** (`auto` es allí `source`), `./build.sh deps` compila ninja, CMake, Python 3.14 (con OpenSSL, zstd y xz),
  pkgconf, SPIRV-Tools, glslang y LLVM 22 con clang desde fuentes fijadas con `build/toolchain.sh`, en
  `<directorio de trabajo>/tools/toolchain`: **no hace falta Homebrew** y nada se instala fuera del directorio de
  trabajo. En un Mac Intel build.sh no usa Homebrew, aunque esté instalado: una copia de esas herramientas que
  ofrezcan Homebrew o el `PATH` no se usa (`doctor` la nombra) y no se llama a `brew`; solo tus propios ajustes
  (`NM_PYTHON`, `NM_LLVM_PREFIX`, `NM_SPIRV_TOOLS_PREFIX`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `NVK_PATH`) se toman en
  su lugar, y `doctor` dice que son tuyos. Solo GNU patch no se compila: si el `patch` de macOS no puede crear archivos (los parches de NVK lo
  necesitan), pon GNU patch en el `PATH` como `gpatch` (la fórmula de Homebrew es `gpatch`); `deps` lo dice. En
  **Apple silicon** (`auto` es allí `homebrew`) una herramienta que ya tengas se usa donde esté (un ajuste como
  `NM_LLVM_PREFIX`, Homebrew, el `PATH`) y `deps` instala la que falte de las fórmulas listas de Homebrew.
- Homebrew (solo con `NM_TOOLS_FROM=homebrew`, el valor de `auto` en Apple silicon), para NVK, el cargador de Vulkan y el traductor: rm, kms, nvrm, fb, accel, agdc, nmsmn, ryzenadj,
  airdis, dlss, osver, el plugin y nvencvt se compilan con Xcode y lo que trae macOS (su `patch` y su `python3` 3.9
  bastan; [tabla](BUILDING.md#tools-each-component-needs)). Los scripts reciben ese `patch` con `-f` añadido
  (`<directorio de trabajo>/tools/bin/patch`): sin él, el `patch` de macOS pregunta en el terminal cuando no encuentra
  el archivo de un parche, aunque su salida vaya a otro sitio, y el paso `rm` se quedaría esperando una respuesta a una
  pregunta que nadie ve. Si falta Homebrew, `./build.sh deps` explica cómo instalarlo y
  ofrece ejecutar su instalador oficial, pero solo después de que escribas `yes` (`--yes` no cuenta para esto).
- Tiempo y disco de las herramientas compiladas desde fuentes, medidos en un Mac Intel de 12 hilos: unos 70 minutos en
  total (11 de descargas, 34 solo LLVM) y 6,7 GB con todas las carpetas de compilación (fuentes 3,2 GB, compilación
  2,4 GB, herramientas instaladas 1,1 GB). `deps` pide 10 GB libres antes de empezar LLVM y dice cuánto queda libre si
  hay menos. Mantén el Mac despierto y enchufado: una herramienta interrumpida (Ctrl-C, reposo, corte) se vuelve a
  compilar desde el principio con el siguiente `./build.sh deps`, y LLVM es la larga. Una herramienta que falla detiene
  `deps` y nombra su registro (`<directorio de trabajo>/logs/toolchain/<herramienta>.log`); el siguiente `deps` empieza
  por ella. Homebrew ya no tiene compilaciones listas (bottles) para Mac Intel: con `NM_TOOLS_FROM=homebrew`, `deps` hace
  que Homebrew compile aquí cada fórmula que falte. Solo LLVM 22 tarda horas y ocupa decenas de gigabytes
  mientras compila. `deps` lo avisa antes de preguntar. En Apple silicon Homebrew instala compilaciones listas. Para
  compilar, Homebrew necesita además de Xcode las herramientas de línea de comandos de Apple, tan nuevas como este
  macOS: sin ellas se detiene en la primera fórmula con `Xcode alone is not sufficient` o `Your Command Line Tools are
  too outdated`. `xcode-select --install` las instala y Actualización de software las actualiza;
  `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables` muestra la versión que tienes.

## Inicio rápido

Clona el repositorio y la rama de los que viene este archivo, con su propietario y el nombre de la rama en lugar de
`<owner>` y `<branch>`. El repositorio del proyecto original no tiene `build.sh`; un clon de él da un árbol sin el
script.

```bash
git clone -b <branch> https://github.com/<owner>/nvidia-macos-driver.git
cd nvidia-macos-driver
./build.sh doctor
./build.sh
```

`./build.sh doctor` no cambia nada tuyo (solo escribe un archivo temporal propio y la caché de `xcrun`; ver más
abajo): muestra lo que este Mac tiene, lo que falta y lo que es demasiado antiguo, y
termina con código 1 si falta algo necesario. `./build.sh` (lo mismo que `./build.sh all`) ejecuta doctor, instala lo
que falta después de preguntar, descarga las fuentes fijadas, compila todos los componentes e imprime un resumen. Si
lo vuelves a ejecutar después de un cambio, los pasos cuyas entradas no cambiaron se saltan.

Todo lo que `build.sh` crea, descarga o instala para sí mismo va al directorio de trabajo, por defecto
`~/nullmoth-build` (fuera del clon); las herramientas compiladas desde fuentes van a su `tools/toolchain`, sus
carpetas de compilación a `tools/toolchain-build`, sus registros a `logs/toolchain` y sus fuentes verificadas a
`src/git/tc-*.git` y `src/toolchain`. El sello de cada herramienta (`tools/toolchain/.nm-toolchain/<herramienta>`)
guarda su commit fijado y su receta: si cambian, `deps` vuelve a compilar esa herramienta y la que la lleva enlazada (Python
después de OpenSSL, zstd o xz; glslang después de SPIRV-Tools), y ninguna otra. Ninguna de ellas se distribuye: la
compilación de NVK tiene LLVM y SPIRV-Tools desactivados y solo `mesa_clc`, un programa que se usa al compilar, los
enlaza. Solo las fórmulas de Homebrew
(`NM_TOOLS_FROM=homebrew`) van a la carpeta de Homebrew. Nada se escribe en
tus archivos de inicio del shell, `~/.cargo`, `~/.rustup` ni `/Library`. Un Rust que tengas en otro sitio (en
`~/.cargo`, por ejemplo) no se usa ni se toca: `doctor` dice que falta Rust hasta que `deps` pone el fijado en el
directorio de trabajo. Pero cargo lee el `.cargo/config.toml` de cada carpeta por encima de aquella en la que trabaja:
con el directorio de trabajo o el clon dentro de tu carpeta personal, tu `~/.cargo/config.toml` se aplica a la
descarga y la compilación del traductor (sus ajustes de perfil, enlazador y fuentes cambian lo que se compila), y
`doctor` lo nombra.

`doctor`, `env`, `help` y cualquier comando con `--dry-run` no escriben nada tuyo: no ejecutan `brew info` ni
`brew install`, leen el clon sin actualizar su índice y no guardan cambios locales con `git stash create`. Cada uno
crea un archivo temporal propio (y una carpeta a su lado para probar `patch`) en tu carpeta temporal (`$TMPDIR`) y los
borra al terminar; `xcrun` guarda su caché de búsqueda (`xcrun_db`) en esa misma carpeta temporal, como hace siempre que
se ejecuta una herramienta de Xcode. `fetch` ejecuta git sin tus hooks (`core.hooksPath`), sin monitor de archivos ni
archivo global de atributos, y `git init` y `git clone` sin tu carpeta de plantilla (`init.templateDir`). `deps`
escribe en la carpeta de Homebrew, en su caché (`~/Library/Caches/Homebrew`) y en sus registros
(`~/Library/Logs/Homebrew`) al instalar, y mientras Homebrew compila una fórmula usa una carpeta en `/private/tmp` que
borra al terminarla; sus preguntas `brew info` (ninguna con `--offline` o `--no-install`) usan una caché en
`<directorio de trabajo>/tools/homebrew-cache` y pip guarda sus descargas en
`<directorio de trabajo>/tools/pip-cache`. Cada `brew install` se ejecuta con `HOMEBREW_NO_INSTALL_CLEANUP=1`: Homebrew
no sigue la instalación con su limpieza propia, que borraría versiones anteriores de tus fórmulas (cada 30 días, de todas),
fórmulas que ya nada necesita y enlaces rotos o carpetas vacías de su prefijo. Homebrew instala también lo que esas
fórmulas necesitan y enlaza en su `bin` los comandos de las que no son keg-only (`llvm@22` lo es), que el `PATH` por
defecto de macOS pone antes de `/usr/bin` en un Mac Intel; hoy eso incluye `python@3.15`, que `llvm@22` necesita para
compilarse y que pasaría a ser el `python3` de una shell con ese `PATH` (la compilación usa `python@3.14`, desde la
carpeta `opt` de Homebrew). Por eso, cuando las instalaciones dan al `bin` de Homebrew un `python3` que no tenía,
enlazado a una fórmula de Python que ellas añadieron o que un `deps` anterior de este directorio de trabajo
desenlazó (las anota en `<directorio de trabajo>/tools/.nm-brew-unlinked` y quita la que vuelvas a enlazar tú),
`deps` ejecuta `brew unlink` con esa fórmula al terminarlas (o
al fallar una, o si paras `deps` con Ctrl-C durante ellas) e imprime una línea con la vuelta atrás (`brew link <fórmula>`); un `python3` que ya estaba (un enlace que no llevaba a ninguna parte no cuenta: tu shell lo saltaba, y Homebrew lo
reemplaza), o una fórmula de Python que ya tenías, se quedan como están. Un `brew install` que termina con error aunque su fórmula ya está instalada (Homebrew no
enlazó un comando sobre un archivo tuyo que estaba en medio, y lo dice) no detiene `deps`: avisa y sigue, y sus
últimas líneas dicen si aún falta algo. Después de instalar, `deps` nombra cada comando que ellas añadieron al `bin` de
Homebrew y que macOS también tiene en `/usr/bin`, `/bin`, `/usr/sbin` o `/sbin`, con el `brew unlink` que lo devuelve.
Una prueba en seco (`--dry-run`) dice esta regla y no llama a `brew`. La tabla completa está en
[BUILDING.md](BUILDING.md#what-each-command-writes).

Los scripts de 2.9 ponen sus árboles en `<directorio de trabajo>/build/...` con nombres que fijan ellos mismos; por
eso el árbol NVIDIA **610.57.04** está en `build/ogkm620/open-gpu-kernel-modules-610.57.04`. Un `build.sh` anterior
los ponía en `<directorio de trabajo>/build/build/...`; esa carpeta ya no se usa y `./build.sh clean` la borra. Detalles en
[BUILDING.md](BUILDING.md#the-working-directorys-layout).

Para hacer un paquete instalable hace falta el paquete publicado de la versión 2.9 como base, porque algunos archivos
que `install.sh` necesita no tienen código fuente en este repositorio (otra versión se rechaza, y también un archivo
que lleve algo más que carpetas, archivos normales y enlaces a un archivo de su misma carpeta). El paquete resultante
no lleva el nombre de tu cuenta en sus cabeceras ni los atributos extendidos de tu Mac, así que se puede pasar a otros:

```bash
./build.sh --base-package ~/Downloads/nullmoth-nvidia-2.9.tar.gz package
cd ~/nullmoth-build/package/pkgroot
sudo env CHECK=1 ./install.sh
sudo ./install.sh && sudo shutdown -r now
```

La primera línea con `install.sh` es una prueba: revisa el paquete y el Mac sin cambiar nada. Antes de instalar, lee
en [README.md](../README.md) "Install — by hand" (OpenCore debe estar configurado). Para volver atrás, instala de la
misma forma el paquete publicado que usaste como base, desde su propio `pkgroot`; para quitar el driver por completo:
`sudo ./uninstall.sh && sudo shutdown -r now`. Los detalles están en [BUILDING.md](BUILDING.md#installing-the-result-and-going-back).

## Comandos

<!-- reference: commands -->
| Comando | Qué hace |
|---|---|
| `all` | Por defecto. doctor, deps si falta algo (salvo con `--no-install`), fetch, compilar todo, el paquete si hay paquete base, y un resumen |
| `doctor` | Solo examina; no cambia nada tuyo. Código 1 si falta algo necesario o es demasiado antiguo |
| `deps` | Instala lo que falta: fórmulas de Homebrew (después de un sí, o `--yes`), SPIRV-LLVM-Translator para LLVM 22, el entorno de Python y Rust (estos tres en el directorio de trabajo) |
| `fetch` | Descarga las fuentes fijadas y verifica cada commit o SHA-256 |
| `build [componente...]` | Compila los componentes indicados y lo que necesitan, o todos |
| `package` | Prepara un paquete a partir de un paquete publicado (`--base-package`); necesita `--accel both` |
| `test` | La comparación de versiones fijadas, las pruebas (`tools/run_all_tests.py`) y la cadena de parches de NVRM. Las pruebas del plugin que compilan con las cabeceras de Vulkan reciben `NM_VULKAN_INCLUDE`: la tuya si la defines, si no el `include` del Mesa descargado (la carpeta con la que se compila el plugin de la versión publicada); una línea dice cuál, o que no hay ninguna hasta `./build.sh fetch` |
| `env` | Imprime líneas `export` con todas las variables que `build.sh` pasa a los scripts (tu carpeta personal como `"$HOME/..."`; el resto de la salida la escribe como `~`) |
| `clean [outputs\|sources\|tools\|all]` | Borra solo dentro del directorio de trabajo (por defecto `outputs`, que también borra las carpetas de compilación y las fuentes exportadas de las herramientas compiladas desde fuentes, no esas herramientas; `tools` las borra sin preguntar, y en un Mac Intel el siguiente `deps` tarda cerca de una hora en volver a compilarlas; `all` pregunta antes) |
| `help` | Comandos, componentes y ajustes |
<!-- end -->

## Opciones y ajustes

Cada opción es también una variable de entorno y una línea del archivo de configuración
(`~/.config/nullmoth/build.conf` si existe, o el que indiquen `--config` o `NM_CONFIG`; ejemplo en
[build/build.conf.example](../build/build.conf.example)). La opción gana sobre el entorno, el entorno sobre el
archivo y el archivo sobre el valor por defecto. El archivo se lee, nunca se ejecuta, y un nombre desconocido detiene
`build.sh`.

<!-- reference: options -->
| Opción | Variable | Por defecto | Significado |
|---|---|---|---|
| `--workdir DIR` | `NM_WORK` | `~/nullmoth-build` | Directorio de trabajo; fuera del clon, sin espacios, sin `: ; ' " \ $` ni comilla invertida |
| `-j N`, `--jobs N` | `NM_JOBS` | número de núcleos | Trabajos de compilación en paralelo |
| `-y`, `--yes` | `NM_ASSUME_YES` | `0` | Responder sí a las preguntas de `build.sh` (nunca al instalador de Homebrew) |
| `--no-install` | `NM_NO_INSTALL` | `0` | No instalar nada; decir qué falta |
| `--offline` | `NM_OFFLINE` | `0` | Sin red: solo lo que dejó un `fetch` anterior. NVK no se puede compilar sin red |
| `-n`, `--dry-run` | `NM_DRY_RUN` | `0` | Imprimir cada comando que se ejecutaría, sin cambiar nada |
| `--flavour release\|dev`, `--flavor` | `NM_FLAVOUR` | `release` | `release`: lo que publica una versión. `dev`: el plugin con sus diagnósticos, para buscar un fallo |
| `--accel 15\|26\|both` | `NM_ACCEL` | `both` | NVAccel.kext para macOS 15, 26 o ambos (un paquete necesita ambos) |
| `-v`, `--verbose` | `NM_VERBOSE` | `0` | Mostrar la salida de cada paso además de guardarla en su registro |
| `--force` | `NM_FORCE` | `0` | Recompilar aunque esté al día |
| `--base-package FILE` | `NM_BASE_PACKAGE` | ninguno | El paquete publicado que sirve de base |
| `--config FILE` | `NM_CONFIG` | `~/.config/nullmoth/build.conf` si existe | El archivo de configuración |
| `-h`, `--help` | | | Lo mismo que `help` |
<!-- end -->

Variables sin opción (su significado completo está en [BUILDING.md](BUILDING.md#settings-without-an-option)):

<!-- reference: variables -->
| Variable | Para qué |
|---|---|
| `NM_BASE_PACKAGE_SHA256` | SHA-256 del paquete base, comprobado antes de usarlo |
| `NM_PACKAGE_VERSION` | Versión escrita en `VERSION` del paquete, que install.sh guarda en `/Library/NullMoth/driver-version` (la versión instalada que leen la búsqueda de actualizaciones de la app y Send logs). Por defecto, la del primer encabezado de CHANGELOG.md (`3.0`), no la del paquete base; `package` se detiene si ese encabezado no es una versión y la variable no está puesta, y también si la variable tiene algo más que cifras y puntos (install.sh solo guarda esos: `3.0rc1` quedaría como `3.01`) |
| `NM_OGKM_URL` | Réplica de open-gpu-kernel-modules de NVIDIA |
| `NM_MESA_URL` | Réplica de Mesa |
| `NM_KHRONOS_URL` | Prefijo de URL con `Vulkan-Headers` y `Vulkan-Loader` |
| `NM_OPEN_GPU_DOC_URL` | Réplica de open-gpu-doc |
| `NM_RYZENADJ_URL` | Réplica de RyzenAdj |
| `NM_WEBKIT_SPI_URL` | Carpeta (URL) con las dos cabeceras de WebKit |
| `NM_SPIRV_LLVM_URL` | Réplica de SPIRV-LLVM-Translator (deps lo compila para LLVM 22) |
| `NM_SPIRV_HEADERS_URL` | Réplica de SPIRV-Headers |
| `NM_TOOLS_FROM` | De dónde salen las herramientas de compilación: `source` las compila con `build/toolchain.sh` desde las fuentes fijadas en `<workdir>/tools/toolchain` y usa solo esas (o tus propios ajustes): una copia que ofrezcan Homebrew o el `PATH` no se usa y no se llama a `brew`; `homebrew` usa la que ya está donde esté e instala la fórmula de Homebrew para la que falte; `auto` (por defecto) es `source` en un Mac Intel (Homebrew no tiene compilaciones listas para él) y `homebrew` en Apple silicon. Otro valor detiene `build.sh` |
| `NM_TC_NINJA_URL` | Réplica de ninja (v1.13.2), fuente de las herramientas que compila deps |
| `NM_TC_CMAKE_URL` | Réplica de CMake (v4.4.5), fuente de las herramientas que compila deps |
| `NM_TC_OPENSSL_URL` | Réplica de OpenSSL (openssl-3.5.9), fuente de las herramientas que compila deps |
| `NM_TC_ZSTD_URL` | Réplica de zstd (v1.5.7), fuente de las herramientas que compila deps |
| `NM_TC_XZ_URL` | Réplica de xz (v5.8.4), fuente de las herramientas que compila deps |
| `NM_TC_PYTHON_URL` | Réplica de CPython (v3.14.10), fuente de las herramientas que compila deps |
| `NM_TC_PKGCONF_URL` | Réplica de pkgconf (pkgconf-2.5.1), fuente de las herramientas que compila deps |
| `NM_TC_SPIRVHEADERS_URL` | Réplica de SPIRV-Headers (para el SPIRV-Tools de la cadena de herramientas) (vulkan-sdk-1.4.363.0), fuente de las herramientas que compila deps |
| `NM_TC_SPIRVTOOLS_URL` | Réplica de SPIRV-Tools (vulkan-sdk-1.4.363.0), fuente de las herramientas que compila deps |
| `NM_TC_GLSLANG_URL` | Réplica de glslang (16.6.0), fuente de las herramientas que compila deps |
| `NM_TC_LLVM_URL` | Réplica de llvm-project (llvmorg-22.1.8), fuente de las herramientas que compila deps |
| `NM_RUSTUP_URL` | De dónde se descarga `rustup-init` |
| `RUSTUP_DIST_SERVER` | De dónde descarga rustup el compilador |
| `DEVELOPER_DIR` | Otro Xcode distinto del seleccionado |
| `NM_BREW` | El `brew` que se usa. Si se indica, es el único Homebrew que se considera, sea cual sea su arquitectura. Sin él, `build.sh` usa solo el Homebrew del procesador del Mac (`/usr/local` en Intel, `/opt/homebrew` en Apple silicon, también desde una shell bajo Rosetta), y si no, un `brew` del `PATH` solo cuando se resuelve dentro de ese mismo prefijo (un envoltorio que no está en ningún prefijo de Homebrew, o uno que se resuelve en el prefijo de la otra arquitectura, no se toma). Un Homebrew de la otra arquitectura, cuyas fórmulas están compiladas para el otro procesador y no se pueden enlazar aquí, lo nombra `doctor` pero solo se usa si `NM_BREW` lo indica. Cada llamada a `brew` se ejecuta con `HOMEBREW_PREFIX`, `HOMEBREW_CELLAR`, `HOMEBREW_REPOSITORY`, `HOMEBREW_PATH` y `HOMEBREW_BREW_FILE` sin definir y con `PATH` igual al `bin` de ese Homebrew seguido de `/usr/bin:/bin:/usr/sbin:/sbin`, para que la configuración de otro Homebrew en la shell no pueda redirigirla: Homebrew guarda el `PATH` de quien lo llama para el paso posterior a la instalación de una fórmula y para algunas de sus compilaciones, donde los programas de ese otro Homebrew irían si no primero. Una herramienta tomada del `PATH` (`python3`, `patch`, `gpatch`, `ninja`, `cmake`, `pkg-config`, `glslangValidator`) se descarta si pertenece al Homebrew de la otra arquitectura (su archivo, tras seguir sus enlaces, está allí; o lo está su intérprete `#!`; o `file` dice que es un programa Mach-O sin código de este procesador); `doctor` dice cuál y por qué |
| `NM_PYTHON` | Un Python 3.10 o posterior para el entorno virtual |
| `NM_LLVM_PREFIX` | Un LLVM 22.x con sus bibliotecas de clang |
| `NM_SPIRV_TOOLS_PREFIX` | Un SPIRV-Tools 2024.1 o posterior que no es el de Homebrew: el prefijo cuyo `lib/pkgconfig` contiene `SPIRV-Tools.pc` |
| `NM_OGKM620_PRISTINE` | Un árbol NVIDIA 620.70.06 ya descomprimido |
| `NM_OGKM620` | Un árbol 620.70.06 con el RM ya compilado (se saltan `rm` y `kms`) |
| `NM_OGKM610` | Un árbol NVIDIA 610.57.04 ya descomprimido |
| `NM_MESA_GIT` | Un clon de Mesa con el commit 90a959eb |
| `NM_VULKAN_INCLUDE` | Cabeceras de Vulkan para el plugin de desarrollo y para las pruebas de `./build.sh test` |
| `NM_LILU_DEBUG_KEXT` | Para `nmkbl` y `nmhda` |
| `NM_VIRTUALSMC_SRC` | Para `nmkbl` |
| `NM_MACKERNELSDK` | Para `nmkbl` y `nmhda` |
| `NULLMOTH_BUILD_ROOT` | Carpeta de compilación de los scripts existentes (por defecto `<workdir>/build`) |
| `NULLMOTH_BUILD_HOST` | El Mac de compilación de los scripts de publicación (por defecto este Mac) |
| `NM_LOCAL_HOST_PATH` | PATH de los comandos que esos scripts ejecutan en este Mac |
| `NV_VERSION` | Versión de NVIDIA (debe ser la fijada, 620.70.06) |
| `KMS_ARCHIVE` | build_nvrm.sh |
| `RE` | build_nvrm.sh y accel_build.sh |
| `MINVER` | fb_build.sh |
| `FB_DEFS` | fb_build.sh |
| `KHDR` | build_primary_selector.sh |
| `KLIB` | build_primary_selector.sh |
| `MACOSX_DEPLOYMENT_TARGET` | Versión mínima de macOS (por defecto 15.0) |
| `NVK_PATH` | build_nvk.sh |
| `NVK_SYSROOT` | build_nvk.sh |
| `NVK_JOBS` | build_nvk.sh |
| `NVK_EXTRA_PATCHES` | build_nvk.sh |
| `NVK_SKIP_PATCHES` | build_nvk.sh |
| `NVK_REF_EXPORTS` | build_nvk.sh |
| `PKG_CONFIG_PATH` | Se pone detrás del que arma `build.sh`, sin sus carpetas dentro del Homebrew de la otra arquitectura |
| `LIBCLANG_PATH` | libclang para bindgen |
| `JOBS` | build_rm620.sh y build_kms620.sh |
| `NULLMOTH_RUST_VERSION` | build_xlate_release.sh (por defecto 1.98.1) |
| `NULLMOTH_RUST_BIN` | build_xlate_release.sh |
| `NULLMOTH_CARGO_HOME` | build_xlate_release.sh |
<!-- end -->

Variables que `build.sh` calcula y pasa a los scripts (las muestra `./build.sh env`):

<!-- reference: variables -->
| Variable | Valor |
|---|---|
| `OGKM` | el árbol 620.70.06 compilado |
| `NVRM_PRISTINE_OGKM` | el árbol 620.70.06 original |
| `RUSTUP_HOME`, `CARGO_HOME`, `RUSTUP_TOOLCHAIN` | Rust 1.98.1 en el directorio de trabajo |
| `NM_LOCAL_BUILD_HOST` | el nombre que atiende `build/local_build_host.sh` |
| `CARGO_NET_OFFLINE` | `true` con `--offline` |
| `PYTHONDONTWRITEBYTECODE` | `1` |
| `SDKROOT` | el SDK de macOS del Xcode elegido (`xcrun --sdk macosx --show-sdk-path`), nunca el tuyo; también para compilar SPIRV-LLVM-Translator y para pip. Sin él, el clang de Apple busca en `/usr/local/include` y `/usr/local/lib` (el Homebrew de un Mac Intel) antes que en el SDK. No se pone donde no hay xcrun. Tu `SDKROOT` y tu `TOOLCHAINS` tampoco llegan al propio `build.sh`: el `git`, el `make` y el `python3` de `/usr/bin` buscan la herramienta real con xcrun, y `build.sh` les pregunta antes de su primer paso; con un `SDKROOT` que nombraba un SDK que no existía, una compilación en un Mac se detuvo ahí. doctor los nombra cuando están puestos; para compilar con otro SDK se elige su Xcode con `DEVELOPER_DIR` |
| `PATH` | `<workdir>/tools/host`, `<workdir>/tools/bin`, `<workdir>/tools/venv/bin`, `/usr/bin:/bin:/usr/sbin:/sbin` |
| `NM_PACKAGE_NAME`, `NM_BUILD_INFO` | nombre del paquete y descripción de la compilación |
<!-- end -->

## Componentes

<!-- reference: components -->
| Componente | Qué es |
|---|---|
| `rm` | el núcleo RM de NVIDIA para macOS, con los parches de kexts/NVRM/patches |
| `kms` | el núcleo de modeset de NVIDIA |
| `nvrm` | NVRM.kext |
| `fb` | NVRMFB.kext |
| `accel` | NVAccel.kext para macOS 15 y 26 |
| `agdc` | NVRMAGDC.kext |
| `nmsmn` | NMSMN.kext |
| `ryzenadj` | ryzenadj |
| `airdis` | nvmtl-air-dis |
| `dlss` | dlss_cache y nm_unzstd |
| `osver` | libnvmtl_osver.dylib |
| `nvk` | NVK, el driver Vulkan |
| `boost` | nvmtl-boost |
| `vulkan` | el cargador de Vulkan de Khronos |
| `plugin` | el plugin de Metal (NVMTLDriver) |
| `xlate` | el traductor de shaders |
| `nvencvt` | libnvencvt.dylib (video) |
| `selector` | NVPrimarySelector.kext (opcional) |
| `nmkbl` | NMKbdBacklight.kext (opcional) |
| `nmhda` | NMHDA.kext (opcional) |
<!-- end -->

Se compilan en el orden de la tabla. Grupos: `all`, `kexts` y `userland`. Ejemplos: `./build.sh build fb`,
`./build.sh --force build plugin`, `./build.sh --flavour dev build plugin`.

## Solución de problemas

| Mensaje o síntoma | Causa | Qué hacer |
|---|---|---|
| `STOP: the working directory contains a space` (o la ruta del clon, o la carpeta personal) | Los scripts de publicación y build_nvk.sh separan por espacios | Usa `--workdir` con una ruta sin espacios; mueve el clon |
| `STOP: the working directory contains one of : ; ' " \ $` (o una comilla invertida) | El `PATH` y las rutas de búsqueda de las herramientas se separan por los dos puntos (las de CMake por el punto y coma), y pkg-config lee las comillas y barras invertidas de sus rutas como comillas | Usa `--workdir` con una ruta sin ellos |
| `STOP: this Mac runs macOS 14...` | Xcode 26 necesita macOS 15.6 o posterior | Actualiza macOS o compila en otro Mac |
| doctor: `MISSING full Xcode 26.0 or later: only the command line tools are selected` | `xcode-select` apunta a las herramientas de línea de comandos | Instala Xcode 26 y ejecuta `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` |
| `STOP: the developer folder xcode-select -p names, ..., does not exist` (o `holds none of Apple's tools`, o `DEVELOPER_DIR names`) | El Xcode seleccionado se borró o se renombró, `DEVELOPER_DIR` indica una carpeta equivocada, o una actualización de macOS dejó incompletas las herramientas de línea de comandos | `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` con el Xcode que tengas, o corrige o quita `DEVELOPER_DIR` |
| doctor: `MISSING Metal/MTL4CommandQueue.h` | El Xcode seleccionado tiene un SDK anterior | Selecciona Xcode 26 o define `DEVELOPER_DIR` |
| `Homebrew has no LLVM 22 formula today` | Homebrew retiró `llvm@22` | Instala LLVM 22.x con sus bibliotecas de clang, define `NM_LLVM_PREFIX` y ejecuta otra vez `./build.sh deps` (compila SPIRV-LLVM-Translator para él) |
| `SPIRV-LLVM-Translator ... does not match LLVM` | Su versión debe coincidir con la de LLVM | `./build.sh deps` compila uno que coincide |
| `STOP: step 'spirv-llvm-translator' failed` | Falló la compilación de SPIRV-LLVM-Translator v22.1.8 con LLVM 22 | Repórtalo con el archivo de informe; el registro completo es `logs/spirv-llvm-translator.log` |
| `STOP: the pins and the scripts disagree` | build/pins.sh y un script piden versiones distintas | Ejecuta `./build.sh doctor`; cambia ambos a la vez |
| `... gave commit X for ..., the pin is Y` o `... has SHA-256 ..., the pin is ...` | Una réplica o descarga no es la fuente fijada | Revisa la variable de réplica; no se usó nada de ella |
| `STOP: another build.sh run (pid N) is using ...` | Dos ejecuciones sobre el mismo directorio de trabajo | Espera a que termine o detenla |
| `offline, and ... is not in ... yet` | `--offline` antes de cualquier `fetch` | Ejecuta `./build.sh fetch` una vez con red |
| `offline: NVK needs the network` | meson descarga seis crates de Rust en cada compilación de NVK | Compila NVK con red |
| `<componente> needs: ...` (por ejemplo `a-patch-that-applies-the-chains(gpatch)`, `python3-3.8`, `cmake/ninja`) | Falta una herramienta que usa ese componente o uno que necesita, o no hace lo que le piden los scripts; no se compiló nada | `./build.sh doctor` dice cuál; `./build.sh deps` la instala |
| rm o kms terminan con código 6 | build_rm620.sh y build_kms620.sh se detienen si el archivo de NVIDIA contiene la ruta de tu carpeta personal, tu nombre de cuenta seguido de `@`, o `@` y el nombre corto del equipo: la compilación los filtró. Como palabra suelta solo se informa (una línea `note:` en el registro), porque el texto de NVIDIA tiene palabras como user, admin o test | El registro da la orden que muestra esas líneas. Infórmalo con el archivo de informe (las líneas te nombran: léelas antes de publicarlas) |
| `STOP: NVRM.kext imports ___x86_return_thunk, which no macOS defines` (u otro kext, u otro nombre `___x86_`, `___fentry__` o `mcount`; el paso termina con código 7) | El kext se compiló con una opción hecha para el kernel de Linux, cuya función auxiliar no existe en macOS: enlazaría y nunca cargaría. Se detiene antes de empaquetar nada | Si `KMS_ARCHIVE` está definida, apunta a un archivo de NVKMS compilado con otros comandos: quítala. Si no, compila de nuevo en otra carpeta de trabajo (`--workdir`); si sigue, repórtalo con el archivo de informe |
| `build_rm620.sh reported compile failures` | Falló una compilación del RM de NVIDIA | Lee las líneas `failed:` en `logs/rm.log` |
| un paso se detiene en un límite de avisos (`warn_ceiling`) | Los límites de build/warnings.txt son 0: otro Xcode puede dar un aviso nuevo | Repórtalo con el archivo de informe y tu versión de Xcode |
| `STOP: step 'toolchain-<tool>' failed` (deps) | `build/toolchain.sh` no pudo compilar o comprobar esa herramienta; su registro es `logs/toolchain/<tool>.log` | Lee sus últimas líneas; el siguiente `./build.sh deps` empieza por esa herramienta |
| `... links <library>, which is neither the system's nor the toolchain's`, o `... is a link to <path>, which is neither ...` (en un registro de toolchain) | Una herramienta tomó una biblioteca de otra carpeta, normalmente el `/usr/local` de Homebrew en un Mac Intel, o instaló un enlace que lleva allí; no se escribe su sello y no se usa | Repórtalo con el archivo de informe |
| `... is built for macOS N and later, above MACOSX_DEPLOYMENT_TARGET` (en un registro de toolchain) | Una herramienta se compiló para un macOS más nuevo que el mínimo (15.0) | Repórtalo con el archivo de informe |
| `deps` se interrumpió mientras compilaba LLVM | El árbol a medias no se retoma | Ejecuta `./build.sh deps` otra vez: LLVM se compila de nuevo desde cero (34 minutos en un Mac Intel de 12 hilos); mantén el Mac despierto |
| `building LLVM needs about 10 GB free` | Poco espacio libre donde está el directorio de trabajo | Haz sitio o elige otro directorio con `--workdir` |
| `libvulkan_nouveau.dylib loads a library from outside macOS` | Se enlazó una biblioteca de Homebrew (a menudo libelf en /usr/local en Intel) | Repórtalo con el archivo de informe |
| `the base package does not match its own SHA256SUMS` | El paquete publicado está dañado o modificado | Descárgalo de nuevo |
| `the base package is release <x>, not 2.9` | `package` solo parte de la versión 2.9: los archivos de otra versión que no tienen fuente aquí saldrían bajo la versión de esta compilación | Usa `nullmoth-nvidia-2.9.tar.gz` de Releases |
| `the base package holds a link that leaves its folder` (o `a device, pipe or socket`, `a set-user-id or set-group-id file`) | El archivo no es un paquete publicado: uno publicado solo lleva carpetas, archivos normales y enlaces a un archivo de su misma carpeta | Descarga otra vez el paquete de Releases; no uses ese archivo |
| `a package must carry NVAccel for macOS 15 and 26` | `package` con `--accel 15` o `26` | Usa `--accel both` |

## Cuando falla la compilación

Un paso que falla detiene la ejecución; ningún paso posterior empieza. El mensaje indica el paso, las últimas líneas
de su registro, la ruta del registro completo (`~/nullmoth-build/logs/<paso>.log`) y la ruta de un archivo de informe
(`~/nullmoth-build/reports/failure-<paso>-<hora>.txt`). El informe contiene la versión de macOS y del Mac, las
versiones de Xcode y de cada herramienta, el commit del clon y si tiene cambios locales (con los nombres de los
archivos cambiados), las versiones fijadas, las variables y las últimas 80 líneas del registro del paso. Tu carpeta
personal aparece como `~`. Léelo antes de publicarlo.

Para reportar un fallo: abre un issue, di qué comando ejecutaste y adjunta el archivo de informe. Corrige lo que
indique el mensaje o la tabla de arriba y ejecuta el mismo comando otra vez: los pasos terminados se saltan y el que
falló se repite. `-v` muestra la salida de cada paso mientras se ejecuta.

Además, cada push se compila desde cero en un Mac Intel de GitHub (macOS 15, sin tarjeta NVIDIA, sin instalar ni
cargar nada): `.github/workflows/full-build.yml` ejecuta deps, doctor, fetch, build, package y test en una carpeta
vacía y comprueba que Homebrew no se usó ni cambió. El resultado está en la pestaña Actions del repositorio, flujo
full-build; si falla o se le acaba el tiempo, su último paso muestra los registros. Hasta que haya pasado una vez, su
resultado no detiene ningún cambio.
