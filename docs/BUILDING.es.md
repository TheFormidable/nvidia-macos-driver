# Compilar el driver desde el código fuente

`./build.sh` compila el driver completo en un Mac con un solo comando: examina el Mac, instala lo que falta (después
de preguntar), descarga cada fuente externa en una versión fijada y verificada, compila cada componente en orden y
puede preparar un paquete instalable. Nunca usa `sudo` y nunca instala el driver: al final imprime el comando para
hacerlo. La guía completa, en inglés, es [BUILDING.md](BUILDING.md); aquí están el inicio rápido, la referencia de
comandos y opciones y la solución de problemas.

> **Escrito y probado sin un Mac.** `./build.sh` se ha ejecutado en un solo Mac (Intel, macOS 26.7, Xcode 26.5), y solo
> en parte: funcionaron `doctor`, `env`, `--dry-run` y `fetch`, y compiló el plugin, la biblioteca de vídeo (y el RM por
> el camino) y las herramientas pequeñas; los kexts se detuvieron en su comprobación de GNU patch y Python 3.10, que ese
> Mac no tenía (la comprobación ahora pide solo lo que usan los kexts). Ninguno de los cambios hechos en `build.sh`
> después de esa ejecución se ha ejecutado aún en un Mac: la comprobación de herramientas por componente, la elección
> de Homebrew y de las herramientas que toma del `PATH`, la organización del directorio de trabajo, la lista de
> compilación del RM, la configuración de git de `fetch` y la parada sin Xcode solo se probaron contra los sustitutos.
> Está escrito para el `/bin/bash` 3.2 de macOS y sus herramientas BSD, y se probó en Linux contra
> sustitutos de las herramientas de macOS, Xcode, Homebrew, rustup, los remotos de git y cada script de compilación
> (`tools/test_build_sh.py`). En un sistema que no es macOS solo funcionan `doctor`, `fetch`, `test`, `env` y
> `--dry-run`. `deps` no ha instalado nada real, no ha compilado NVK ni el traductor y no ha preparado ningún paquete.
> Para la primera ejecución esto significa: es probable que algún paso se detenga por algo que ningún sustituto imitó
> (un detalle de Xcode o de Homebrew, el nombre de una fórmula, un aviso del compilador). Ejecuta primero `./build.sh
> doctor` y después `./build.sh`; si un paso se detiene, el mensaje dice cuál es y da un archivo de informe para
> adjuntar al reportar el fallo (ver [Cuando falla la compilación](#cuando-falla-la-compilación)). Un paso que falla
> nunca queda marcado como terminado, así que, una vez corregido, la siguiente ejecución sigue desde ese paso. Los
> scripts de los componentes sí se ejecutaron a mano en el Mac Intel del propietario (macOS 26.7, Xcode 26.5) para los
> kexts y el RM; lo que eso mostró sobre un nombre de cuenta corto ya está resuelto (fila `rm` de la tabla de
> problemas).

## Requisitos

- Un Mac con macOS 15.6 o posterior, Intel o Apple silicon. Xcode 26 no funciona en sistemas anteriores, así que
  `deps`, `build`, `package` y `all` los rechazan al empezar. Apple silicon sirve para compilar; todo se compila para
  x86_64, la arquitectura en la que funciona el driver.
- **Xcode 26 o posterior**, el Xcode completo, seleccionado con `xcode-select`. Las herramientas de línea de comandos
  solas no bastan: el plugin usa cabeceras de Metal 4 que solo tiene el SDK de macOS 26. No hace falta ningún Kernel
  Debug Kit. En un Mac sin Xcode ni herramientas de línea de comandos, todos los comandos salvo `help` se detienen al
  empezar con las instrucciones (`doctor` nombra Xcode como lo único que falta): el `git`, `make`, `python3` y `xcrun`
  de macOS son entonces sustitutos que ofrecen instalar las herramientas de línea de comandos, y no se ejecuta ninguno.
- Un clon git de este repositorio (no un archivo descargado). La ruta del clon, la del directorio de trabajo y la de tu
  carpeta personal no pueden contener espacios.
- Red para `fetch`, para `deps` y para cada compilación de NVK.
- Homebrew, solo para NVK, el cargador de Vulkan y el traductor: rm, kms, nvrm, fb, accel, agdc, nmsmn, ryzenadj,
  airdis, dlss, osver, el plugin y nvencvt se compilan con Xcode y lo que trae macOS (su `patch` y su `python3` 3.9
  bastan; [tabla](BUILDING.md#tools-each-component-needs)). Si falta, `./build.sh deps` explica cómo instalarlo y
  ofrece ejecutar su instalador oficial, pero solo después de que escribas `yes` (`--yes` no cuenta para esto).
- Tiempo, en un **Mac Intel**: Homebrew ya no tiene compilaciones listas (bottles) de estas fórmulas para Mac Intel, así
  que `deps` hace que Homebrew compile aquí cada una que falte. Solo LLVM 22 tarda horas y ocupa decenas de gigabytes
  mientras compila. `deps` lo avisa antes de preguntar. En Apple silicon Homebrew instala compilaciones listas.

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
`~/nullmoth-build` (fuera del clon). Solo las fórmulas de Homebrew van a la carpeta de Homebrew. Nada se escribe en
tus archivos de inicio del shell, `~/.cargo`, `~/.rustup` ni `/Library`.

`doctor`, `env`, `help` y cualquier comando con `--dry-run` no escriben nada tuyo: no ejecutan `brew info` ni
`brew install`, leen el clon sin actualizar su índice y no guardan cambios locales con `git stash create`. Cada uno
crea un archivo temporal propio (y una carpeta a su lado para probar `patch`) en tu carpeta temporal (`$TMPDIR`) y los
borra al terminar; `xcrun` guarda su caché de búsqueda (`xcrun_db`) en esa misma carpeta temporal, como hace siempre que
se ejecuta una herramienta de Xcode. `fetch` ejecuta git sin tus hooks (`core.hooksPath`), sin monitor de archivos ni
archivo global de atributos, y `git init` y `git clone` sin tu carpeta de plantilla (`init.templateDir`). `deps`
escribe en la carpeta de Homebrew y en su caché (`~/Library/Caches/Homebrew`) al instalar, pero sus preguntas
`brew info` usan una caché en `<directorio de trabajo>/tools/homebrew-cache`. La tabla completa está en
[BUILDING.md](BUILDING.md#what-each-command-writes).

Los scripts de 2.9 ponen sus árboles en `<directorio de trabajo>/build/...` con nombres que fijan ellos mismos; por
eso el árbol NVIDIA **610.57.04** está en `build/ogkm620/open-gpu-kernel-modules-610.57.04`. Un `build.sh` anterior
los ponía en `<directorio de trabajo>/build/build/...`; esa carpeta ya no se usa y `./build.sh clean` la borra. Detalles en
[BUILDING.md](BUILDING.md#the-working-directorys-layout).

Para hacer un paquete instalable hace falta un paquete de una versión publicada como base, porque algunos archivos que
`install.sh` necesita no tienen código fuente en este repositorio:

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
| `test` | La comparación de versiones fijadas, las pruebas (`tools/run_all_tests.py`) y la cadena de parches de NVRM |
| `env` | Imprime líneas `export` con todas las variables que `build.sh` pasa a los scripts (tu carpeta personal como `"$HOME/..."`; el resto de la salida la escribe como `~`) |
| `clean [outputs\|sources\|tools\|all]` | Borra solo dentro del directorio de trabajo (por defecto `outputs`; `all` pregunta antes) |
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
| `--workdir DIR` | `NM_WORK` | `~/nullmoth-build` | Directorio de trabajo; fuera del clon y sin espacios |
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
| `NM_PACKAGE_VERSION` | Versión escrita en `VERSION` del paquete |
| `NM_OGKM_URL` | Réplica de open-gpu-kernel-modules de NVIDIA |
| `NM_MESA_URL` | Réplica de Mesa |
| `NM_KHRONOS_URL` | Prefijo de URL con `Vulkan-Headers` y `Vulkan-Loader` |
| `NM_OPEN_GPU_DOC_URL` | Réplica de open-gpu-doc |
| `NM_RYZENADJ_URL` | Réplica de RyzenAdj |
| `NM_WEBKIT_SPI_URL` | Carpeta (URL) con las dos cabeceras de WebKit |
| `NM_SPIRV_LLVM_URL` | Réplica de SPIRV-LLVM-Translator (deps lo compila para LLVM 22) |
| `NM_SPIRV_HEADERS_URL` | Réplica de SPIRV-Headers |
| `NM_RUSTUP_URL` | De dónde se descarga `rustup-init` |
| `RUSTUP_DIST_SERVER` | De dónde descarga rustup el compilador |
| `DEVELOPER_DIR` | Otro Xcode distinto del seleccionado |
| `NM_BREW` | El `brew` que se usa. Si se indica, es el único Homebrew que se considera, sea cual sea su arquitectura. Sin él, `build.sh` usa solo el Homebrew del procesador del Mac (`/usr/local` en Intel, `/opt/homebrew` en Apple silicon, también desde una shell bajo Rosetta), y si no, un `brew` del `PATH` solo cuando se resuelve dentro de ese mismo prefijo (un envoltorio que no está en ningún prefijo de Homebrew, o uno que se resuelve en el prefijo de la otra arquitectura, no se toma). Un Homebrew de la otra arquitectura, cuyas fórmulas están compiladas para el otro procesador y no se pueden enlazar aquí, lo nombra `doctor` pero solo se usa si `NM_BREW` lo indica. Cada llamada a `brew` se ejecuta con `HOMEBREW_PREFIX`, `HOMEBREW_CELLAR` y `HOMEBREW_REPOSITORY` sin definir, para que la configuración de otro Homebrew en la shell no pueda redirigirla. Una herramienta tomada del `PATH` (`python3`, `patch`, `gpatch`, `ninja`, `cmake`, `pkg-config`, `glslangValidator`) se descarta si pertenece al Homebrew de la otra arquitectura (su archivo, tras seguir sus enlaces, está allí; o lo está su intérprete `#!`; o `file` dice que es un programa Mach-O sin código de este procesador); `doctor` dice cuál y por qué |
| `NM_PYTHON` | Un Python 3.10 o posterior para el entorno virtual |
| `NM_LLVM_PREFIX` | Un LLVM 22.x con sus bibliotecas de clang |
| `NM_OGKM620_PRISTINE` | Un árbol NVIDIA 620.70.06 ya descomprimido |
| `NM_OGKM620` | Un árbol 620.70.06 con el RM ya compilado (se saltan `rm` y `kms`) |
| `NM_OGKM610` | Un árbol NVIDIA 610.57.04 ya descomprimido |
| `NM_MESA_GIT` | Un clon de Mesa con el commit 90a959eb |
| `NM_VULKAN_INCLUDE` | Cabeceras de Vulkan para el plugin de desarrollo |
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
| `nvencvt` | libnvencvt.dylib (vídeo) |
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
| `STOP: this Mac runs macOS 14...` | Xcode 26 necesita macOS 15.6 o posterior | Actualiza macOS o compila en otro Mac |
| doctor: `MISSING full Xcode 26.0 or later: only the command line tools are selected` | `xcode-select` apunta a las herramientas de línea de comandos | Instala Xcode 26 y ejecuta `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` |
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
| rm o kms terminan con código 6 | build_rm620.sh y build_kms620.sh se detienen si el archivo de NVIDIA contiene tu nombre de cuenta o el nombre corto del equipo como palabra completa (no dentro de otra palabra) | El registro da la orden que muestra esas líneas. Si el nombre es una palabra común (user, mac, admin), compila desde otra cuenta o cambia el nombre local del equipo (Ajustes del Sistema > General > Compartir) |
| `build_rm620.sh reported compile failures` | Falló una compilación del RM de NVIDIA | Lee las líneas `failed:` en `logs/rm.log` |
| un paso se detiene en un límite de avisos (`warn_ceiling`) | Los límites de build/warnings.txt son 0: otro Xcode puede dar un aviso nuevo | Repórtalo con el archivo de informe y tu versión de Xcode |
| `libvulkan_nouveau.dylib loads a library from outside macOS` | Se enlazó una biblioteca de Homebrew (a menudo libelf en /usr/local en Intel) | Repórtalo con el archivo de informe |
| `the base package does not match its own SHA256SUMS` | El paquete publicado está dañado o modificado | Descárgalo de nuevo |
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
