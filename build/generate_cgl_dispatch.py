#!/usr/bin/env python3
"""generate_cgl_dispatch.py <OpenGL headers dir> <cgl-api-requirements.json> <out dir>

Writes the CGL adapter's dispatch table (cgl_dispatch.inc), its capability lists (cgl_api.inc) and a record of the
mapping (cgl-dispatch-map.json) from the public headers of the macOS SDK's OpenGL.framework and Mesa's Khronos registry.
Nothing here reads a binary.

The layout comes from the SDK: CGLContext.h declares struct _CGLContextObject {GLIContext rend; GLIFunctionDispatch
disp; CGLPrivateObj priv; void *stak;}, and gliDispatch.h declares every member of GLIFunctionDispatch with its
signature. That is the table that applications built with the SDK's CGLMacro.h call through. The generated entries
carry offsetof(struct _CGLContextObject, disp.<member>), so the compiler places each slot from the header, and nothing
here counts slots.

Each member gets the GL names that reach it, in this order, and a name is kept only when its SDK prototype has the
member's signature (after the spellings the two headers use for the same C type are made equal):
  1. CGLMacro.h's pairs: '#define glName(...) (*(CGL_MACRO_CONTEXT)->disp.member)(...)';
  2. the member's own spelling: 'bind_buffer_ARB' is glBindBufferARB, and a member without a suffix may also be the
     ARB or EXT spelling of the same function, when exactly one such SDK prototype exists;
  3. the Khronos registry's alias groups (gl.xml '<alias>'), for a name that 1 and 2 left free and that no member
     spells itself.
A member that keeps no name is not adapted: its slot keeps Apple's entry, which is what the source fork's adapter did
for the slots that no public libGL symbol reaches. An SDK prototype that reaches no member is listed in the record.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opengl_api import supported  # noqa: E402

# the return type ends at a space or at the '*' of 'extern GLvoid *glMapBufferARB(...)' (glext.h's spelling)
PROTOTYPE = (r'(?:GLAPI|extern)\s+([^;{}]+?[\s*])\s*(?:APIENTRY\s+)?(gl\w+)\s*'
             r'\(([^();{}]*?)\)\s*(?:OPENGL_DEPRECATED(?:\([^)]*\))?\s*)?;')
SDK_HEADERS = ('gl3.h', 'gl3ext.h', 'gl.h', 'glext.h')
SUFFIX = ('ARB', 'EXT', 'APPLE', 'ATI', 'NV', 'SGIS', 'SGI', 'IBM', 'OES', 'KHR')
REQUIRED = {'glMapBufferRange', 'glFlushMappedBufferRange', 'glVertexAttribDivisor', 'glBegin', 'glEnd', 'glNewList',
            'glCallList', 'glMatrixMode', 'glActiveTextureARB', 'glClientActiveTextureARB', 'glBindBufferARB',
            'glBindFramebufferEXT', 'glGetAttachedObjectsARB', 'glViewport', 'glFlush', 'glFinish', 'glGetString'}
# members the adapter handles itself (nvk_cgl.c): the drawable swap that CGLFlushDrawable calls
OWN = {'swap_APPLE'}


def strip_comments(text):
    return re.sub(r'/\*.*?\*/|//[^\n]*', ' ', text, flags=re.S)


def collect_prototypes(headers):
    prototypes = {}
    for header in SDK_HEADERS:
        text = strip_comments((headers / header).read_text(errors='replace'))
        # A macro such as '#define GLAPI extern' must not become a return type; continued directives are skipped too.
        lines, continued = [], False
        for line in text.splitlines():
            if continued or line.lstrip().startswith('#'):
                continued = line.rstrip().endswith('\\')
            else:
                lines.append(line)
        for ret, name, params in re.findall(PROTOTYPE, '\n'.join(lines)):
            prototypes.setdefault(name, (ret.strip(), name, params.strip()))
    return prototypes


def collect_members(headers):
    text = strip_comments((headers / 'gliDispatch.h').read_text(errors='replace'))
    start = text.index('typedef struct __GLIFunctionDispatchRec')
    body = text[text.index('{', start) + 1:text.index('} GLIFunctionDispatch;', start)]
    members = []
    for decl in [d.strip() for d in body.split(';') if d.strip()]:
        m = re.fullmatch(r'(.+?)\(\s*\*\s*(\w+)\s*\)\s*\(\s*GLIContext\s+\w+\s*(?:,(.*))?\)', decl, re.S)
        if not m:
            raise SystemExit('gliDispatch.h: a member that is not a function pointer taking GLIContext: ' + decl[:80])
        members.append((m.group(2), m.group(1).strip(), (m.group(3) or '').strip()))
    if len({name for name, _, _ in members}) != len(members):
        raise SystemExit('gliDispatch.h: a member name appears twice')
    return members


def macro_pairs(headers):
    text = (headers / 'CGLMacro.h').read_text(errors='replace')
    return re.findall(r'#define\s+(gl\w+)\s*\([^)]*\)\s*\\?\s*\(\s*\*\s*\(\s*CGL_MACRO_CONTEXT\s*\)\s*->\s*disp\.(\w+)\s*\)',
                      text)


def parameters(params):
    """Give the SDK's occasional unnamed parameters local adapter names."""
    if params.strip() in ('void', ''):
        return 'void', []
    declarations, names = [], []
    for i, param in enumerate(params.split(',')):
        param = param.strip()
        match = re.search(r'(\w+)\s*(?:\[.*\])?$', param)
        named = match and not match.group(1).startswith('GL') and match.group(1) not in {'void', 'const'}
        name = match.group(1) if named else f'arg{i}'
        declarations.append(param if named else param + ' ' + name)
        names.append(name)
    return ', '.join(declarations), names


def signature(ret, params):
    params, names = parameters(params)
    types = [] if params == 'void' else [re.sub(r'\b' + name + r'\b', '', param)
                                         for param, name in zip(params.split(','), names)]
    types = [re.sub(r'\[\s*\d*\s*\]', '*', t) for t in types]
    value = ret + ':' + ','.join(types)
    # The C type each GL name stands for (the SDK's gltypes.h and glext.h), reduced to what the x86_64 calling
    # convention and the pointed-to element see: 32-bit integers of either sign pass alike, as do 64-bit ones and
    # intptr_t, while 8- and 16-bit ones keep their sign (the caller extends them) and floats their width. Apple's
    # GLhandleARB is a pointer that carries a 32-bit name: as an argument or a result it passes in the same register
    # as a GLuint, and libGL sends the core shader calls through the ARB members (CGLMacro.h: glDeleteProgram is
    # disp.delete_object_ARB). An array of handles is not an array of GLuint, so GLhandleARB * stays apart.
    value = re.sub(r'\bGLhandleARB\s*\*', 'handle*', value)
    for names, base in ((('GLbitfield', 'GLenum', 'GLuint', 'GLint', 'GLsizei', 'GLfixed', 'GLhandleARB'), 'w32'),
                        (('GLboolean', 'GLubyte'), 'u8'), (('GLbyte',), 'i8'), (('GLshort',), 'i16'),
                        (('GLushort', 'GLhalf', 'GLhalfARB'), 'u16'), (('GLfloat', 'GLclampf'), 'f32'),
                        (('GLdouble', 'GLclampd'), 'f64'),
                        (('GLintptr', 'GLsizeiptr', 'GLintptrARB', 'GLsizeiptrARB', 'GLint64', 'GLint64EXT', 'GLuint64',
                          'GLuint64EXT'), 'w64'),
                        (('GLchar', 'GLcharARB'), 'char'), (('GLvoid',), 'void')):
        for old in names:
            value = re.sub(r'\b' + old + r'\b', base, value)
    return re.sub(r'\s+', '', re.sub(r'\bconst\b', '', value))


def camel(member):
    return 'gl' + ''.join(p[:1].upper() + p[1:] for p in member.split('_'))


def squash(name):
    return name.replace('_', '').lower()


def alias_groups(contract):
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for name, target in contract.get('aliases', {}).items():
        parent[find(name)] = find(target)
    groups = {}
    for name in list(parent):
        groups.setdefault(find(name), set()).add(name)
    return {name: group for group in groups.values() for name in group}


def build_map(prototypes, members, pairs, contract):
    sig = {n: signature(r, p) for n, (r, _, p) in prototypes.items()}
    msig = {m: signature(r, p) for m, r, p in members}
    by_squash = {}
    for n in prototypes:
        by_squash.setdefault(squash(n), []).append(n)
    owner, how, rejected = {}, {}, {}

    def take(name, member, why):
        if name in owner or name not in prototypes:
            return
        if sig[name] != msig[member]:
            rejected[name] = f'{why}: disp.{member} has {msig[member]}, the SDK prototype {sig[name]}'
            return
        owner[name] = member
        how[name] = why
    for name, member in pairs:
        if member in msig:
            take(name, member, 'CGLMacro.h')
    spelled = set()
    for member in msig:
        own = by_squash.get(squash(camel(member)), [])
        spelled.update(own)
        for name in own:
            take(name, member, 'member spelling')
        if not re.search(r'_(?:' + '|'.join(SUFFIX) + r')$', member):
            ext = [n for s in ('ARB', 'EXT') for n in by_squash.get(squash(camel(member) + s), [])]
            if len(ext) == 1:
                take(ext[0], member, 'member spelling with ' + ext[0][-3:])
    # A member that carries a suffix the SDK prototype dropped: bind_vertex_array_ARB is gl3.h's glBindVertexArray.
    # Taken when no member spells that name itself, when the registry has an extension of that suffix provide the
    # name (GL_ARB_vertex_array_object requires glBindVertexArray; no GL_EXT_ extension does, so
    # bind_vertex_array_EXT, which CGLMacro.h gives glBindVertexArrayAPPLE, is not it), and when one member is left.
    providers = {}
    for profile in contract.get('extensions', {}).values():
        for extension, commands in profile.items():
            for command in commands:
                providers.setdefault(command, set()).add(extension)
    stripped = {}
    for member in msig:
        m = re.fullmatch(r'(.+)_(' + '|'.join(SUFFIX) + r')', member)
        if not m:
            continue
        for name in by_squash.get(squash(camel(m.group(1))), []):
            if name not in spelled and any(e.startswith('GL_' + m.group(2) + '_') for e in providers.get(name, ())):
                stripped.setdefault(name, []).append(member)
    for name, found in stripped.items():
        if len(found) == 1:
            take(name, found[0], 'member spelling without its suffix')
    groups = alias_groups(contract)
    for name in prototypes:
        if name in owner or name in spelled:
            continue
        reached = {owner[a] for a in groups.get(name, ()) if a in owner}
        if len(reached) == 1:
            take(name, reached.pop(), 'Khronos alias')
    slots = {}
    for name, member in owner.items():
        slots.setdefault(member, []).append(name)
    return slots, how, rejected


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    headers, contract_path, output = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    output.mkdir(parents=True, exist_ok=True)
    contract = json.loads(contract_path.read_text())
    prototypes = collect_prototypes(headers)
    members = collect_members(headers)
    pairs = macro_pairs(headers)
    if len(prototypes) < 1000 or len(members) < 900 or len(pairs) < 700:
        raise SystemExit(f'Incomplete SDK OpenGL headers in {headers}: {len(prototypes)} prototypes, '
                         f'{len(members)} dispatch members, {len(pairs)} CGLMacro.h pairs')
    member_names = {m for m, _, _ in members}
    for member in OWN | {'viewport', 'flush', 'finish'}:
        if member not in member_names:
            raise SystemExit('gliDispatch.h has no member ' + member)
    slots, how, rejected = build_map(prototypes, members, pairs, contract)
    for member in OWN:
        slots.pop(member, None)
    mapped = {n for names in slots.values() for n in names}
    if not REQUIRED <= mapped:
        raise SystemExit('Required GL functions reach no dispatch member: ' + ', '.join(sorted(REQUIRED - mapped)))
    order = [m for m, _, _ in members if m in slots]
    for member in order:
        slots[member].sort(key=lambda n: (n != camel(member), len(n), n))
    capabilities = {}
    policy = ['/* SPDX-License-Identifier: Apache-2.0',
              ' * Copyright 2013-2020 The Khronos Group Inc.',
              ' * Generated by build/generate_cgl_dispatch.py: the Khronos registry\'s versions and extensions whose',
              ' * every command reaches an adapted CGL dispatch member (SDK headers). Do not edit. */']
    for profile in ('core', 'compatibility'):
        version, extensions = supported(contract, mapped, profile)
        capabilities[profile] = {'version': version, 'extensions': extensions}
        policy += [f'static const unsigned api_{profile}_version = {version};',
                   f'static const char *const api_{profile}_extensions[] = {{']
        policy += ['  ' + json.dumps(name) + ',' for name in extensions]
        policy += ['};']
    (output / 'cgl_api.inc').write_text('\n'.join(policy) + '\n')
    wrappers = ['/* Generated by build/generate_cgl_dispatch.py from the SDK\'s gliDispatch.h, CGLMacro.h and GL headers.',
                ' * Each slot is offsetof(struct _CGLContextObject, disp.<member>). Do not edit. */']
    rows = []
    for member in order:
        aliases = slots[member]
        ret, name, params = prototypes[aliases[0]]
        params, names = parameters(params)
        slot = f'offsetof(struct _CGLContextObject, disp.{member})'
        with_ctx = 'void *engine' + (', ' + params if params != 'void' else '')
        wrappers += [f'static {ret} nvk_{name}({with_ctx}) {{']
        if member == 'viewport':
            wrappers += ['  if (native_viewport(engine, __builtin_return_address(0), x, y, width, height)) return;']
        wrappers += [f'  {ret} (*nvk_apple)({with_ctx}) = original_noncurrent(engine, {slot});',
                     f'  if (nvk_apple) {{ {"" if ret == "void" else "return "}nvk_apple(engine{", " if names else ""}{", ".join(names)});' +
                     (' return; }' if ret == 'void' else ' }'),
                     '  struct nvk_cgl_bridge *nvk_bridge = bridge_current(engine);',
                     ('  if (!nvk_bridge) return;' if ret == 'void' else '  if (!nvk_bridge) return 0;')]
        if name in {'glDeleteProgram', 'glDeleteShader'}:
            wrappers += [
                '  GLboolean (*is_program)(GLuint) = (void *)nvk_zink_get_proc_address("glIsProgram");',
                '  void (*delete_object)(GLuint) = (void *)nvk_zink_get_proc_address(',
                '     is_program(' + names[0] + ') ? "glDeleteProgram" : "glDeleteShader");',
                '  delete_object(' + names[0] + ');']
        elif name == 'glGetString':
            wrappers += [f'  return capability_string(nvk_bridge, {names[0]});']
        elif name == 'glGetStringi':
            wrappers += [f'  return capability_string_indexed(nvk_bridge, {names[0]}, {names[1]});']
        else:
            # Mesa and Apple use a pointer-sized GLhandleARB on macOS, output arrays included. Shared core slots use
            # the GLuint declarations above; a slot of its own keeps its pointer-sized signature.
            wrappers += [f'  {ret} (*nvk_func)({params}) = (void *)nvk_bridge->procs[{len(rows)}];',
                         f'  if (!nvk_func) {{ missing_entrypoint("{name}");' +
                         (' return; }' if ret == 'void' else ' return 0; }')]
            if name in {'glGetIntegerv', 'glGetInteger64v', 'glGetFloatv', 'glGetDoublev', 'glGetBooleanv'}:
                value = '!!nvk_value' if name == 'glGetBooleanv' else 'nvk_value'
                wrappers += ['  GLint nvk_value;',
                             f'  if (capability_parameter(nvk_bridge, {names[0]}, &nvk_value)) {{ *{names[1]} = {value}; return; }}']
            if set(aliases) & {'glFlush', 'glFinish', 'glFlushRenderAPPLE', 'glFinishRenderAPPLE', 'glFenceSync'}:
                wrappers += ['  if (!prepare_iosurface_flush(nvk_bridge))' +
                             (' return;' if ret == 'void' else ' return 0;')]
            wrappers += [f'  {"" if ret == "void" else "return "}nvk_func({", ".join(names)});']
            if set(aliases) & {'glBindTexture', 'glBindTextureEXT'}:
                wrappers += [f'  refresh_iosurface_bindings(nvk_bridge, {names[0]}, 0);']
            if set(aliases) & {'glBindFramebuffer', 'glBindFramebufferEXT'}:
                wrappers += [f'  refresh_iosurface_bindings(nvk_bridge, {names[0]}, 1);']
            if set(aliases) & {'glFlush', 'glFinish', 'glFlushRenderAPPLE', 'glFinishRenderAPPLE'}:
                wrappers += ['  flush_front_buffer(nvk_bridge);']
        wrappers += ['}']
        rows.append(f'  {{"{name}", {slot}, (void *)nvk_{name}}},')
    wrappers += ['static const struct dispatch_entry entries[] = {', *rows, '};',
                 f'#define NVK_CGL_DISPATCH_MEMBERS {len(members)}']
    (output / 'cgl_dispatch.inc').write_text('\n'.join(wrappers) + '\n')
    unreached = sorted(set(prototypes) - mapped)
    unadapted = [m for m, _, _ in members if m not in slots and m not in OWN]
    (output / 'cgl-dispatch-map.json').write_text(json.dumps({
        'source': 'public macOS SDK headers: OpenGL.framework gliDispatch.h, CGLContext.h, CGLMacro.h, ' +
                  ', '.join(SDK_HEADERS) + '; Khronos registry aliases from Mesa\'s gl.xml',
        'dispatch_members': len(members),
        'adapted_members': {m: {'names': slots[m], 'how': {n: how[n] for n in slots[m]}} for m in order},
        'unadapted_members': unadapted,
        'sdk_prototypes_reaching_no_member': unreached,
        'rejected_for_signature': rejected,
        'capabilities': capabilities,
        'registry_sha256': contract.get('registry_sha256'),
    }, indent=1, sort_keys=True) + '\n')
    print(f'{len(members)} dispatch members: {len(order)} adapted for {len(mapped)} GL names, {len(unadapted)} keep '
          f'Apple\'s entry; {len(unreached)} SDK prototypes reach no member; {len(rejected)} names refused for their '
          f'signature; core {capabilities["core"]["version"]}, compatibility {capabilities["compatibility"]["version"]}')


if __name__ == '__main__':
    main()
