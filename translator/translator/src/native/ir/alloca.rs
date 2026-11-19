use super::*;

impl LlModule {
    pub(in crate::native) fn infer_local_alloca_pointees(&mut self) {
        // From pull request 524 of the original project (League of Legends): only a pointer proven
        // to address the whole alloca (whole_object_aliases) retypes it, and a helper's byte view
        // of a whole pointer-free local is one more view of it. NVMTL_WHOLE_VIEWS=0 keeps the rule
        // of 3.0.4, where a GEP through any pointer derived from the alloca (a member, a dynamic
        // offset, a phi with a null or unknown arm) offered its source type as the alloca's own.
        let whole_views = crate::native::view_switches::whole_views_on();
        for f in &self.functions {
            let allocas = self.local_allocas(f);
            if allocas.is_empty() {
                continue;
            }

            let mut roots: HashMap<String, String> = allocas
                .keys()
                .map(|name| (name.clone(), name.clone()))
                .collect();
            let mut alias_roots: HashMap<String, String> = roots.clone();
            if whole_views {
                alias_roots.extend(whole_object_aliases(f, &allocas));
            }
            let mut sources: HashMap<String, HashSet<LlType>> = HashMap::default();
            let mut changed = true;
            while changed {
                changed = false;
                for inst in f.carrier_insts() {
                    if let Some((res, base)) = inst.identity_ptr_bitcast() {
                        if let Some(root) = roots.get(base).cloned() {
                            if roots.insert(res.to_string(), root).is_none() {
                                changed = true;
                            }
                        }
                        // whole_object_aliases has followed identity casts; 3.0.4 did it here.
                        if !whole_views {
                            if let Some(root) = alias_roots.get(base).cloned() {
                                if alias_roots.insert(res.to_string(), root).is_none() {
                                    changed = true;
                                }
                            }
                        }
                        continue;
                    }

                    let Some(res) = &inst.result else {
                        continue;
                    };
                    if let Some(gep) = &inst.gep() {
                        let LlValue::Local(base) = &gep.base.value else {
                            continue;
                        };
                        let Some(root) = roots.get(base).cloned() else {
                            continue;
                        };
                        // A GEP through a member describes that subobject, not a new view of the
                        // whole alloca: an equal-sized first member (a matrix's column array) must
                        // not replace the storage of the struct around it.
                        if !whole_views || alias_roots.get(base) == Some(&root) {
                            sources
                                .entry(root.clone())
                                .or_default()
                                .insert(gep.source_ty.clone());
                        }
                        if roots.insert(res.clone(), root).is_none() {
                            changed = true;
                        }
                        continue;
                    }

                    if let Some(incoming) = inst.phi_values() {
                        let mut root: Option<String> = None;
                        for value in incoming {
                            let LlValue::Local(name) = value else {
                                continue;
                            };
                            let Some(candidate) = roots.get(name).cloned() else {
                                continue;
                            };
                            match &root {
                                Some(existing) if existing != &candidate => {
                                    root = None;
                                    break;
                                }
                                None => root = Some(candidate),
                                _ => {}
                            }
                        }
                        if let Some(root) = root {
                            if roots.insert(res.clone(), root).is_none() {
                                changed = true;
                            }
                        }
                        continue;
                    }
                }
            }

            for inst in f.carrier_insts() {
                let (ptr, accessed) = if let Some(load) = inst.load().as_deref() {
                    (&load.ptr, load.result_ty.clone())
                } else if let Some((object, ptr)) = inst.store().as_deref() {
                    (ptr, object.ty.clone())
                } else {
                    continue;
                };
                let LlValue::Local(ptr_name) = &ptr.value else {
                    continue;
                };
                let Some(root) = alias_roots.get(ptr_name) else {
                    continue;
                };
                let Some(declared) = allocas.get(root) else {
                    continue;
                };
                let (declared_resolved, accessed_resolved) = (
                    self.resolve_known_type(declared),
                    self.resolve_known_type(&accessed),
                );
                let widens_padding = match (&declared_resolved, &accessed_resolved) {
                    (LlType::Vector(from_elem, from_lanes), LlType::Vector(to_elem, to_lanes)) => {
                        from_elem == to_elem && to_lanes > from_lanes
                    }
                    _ => false,
                };
                let padded_rows =
                    padded_float3_rows_viewed_wide(&declared_resolved, &accessed_resolved);
                if !widens_padding && !padded_rows {
                    continue;
                }
                sources.entry(root.clone()).or_default().insert(accessed);
            }

            for inst in f.carrier_insts() {
                let Some(call) = inst.call().as_deref() else {
                    continue;
                };
                let Some(callee) = self.functions.iter().find(|g| g.name == call.callee) else {
                    continue;
                };
                for (arg, (param_name, _param_ty)) in call.args.iter().zip(callee.params.iter()) {
                    let LlValue::Local(arg_name) = &arg.value else {
                        continue;
                    };
                    let Some(root) = alias_roots.get(arg_name) else {
                        continue;
                    };
                    let Some(declared) = allocas.get(root) else {
                        continue;
                    };
                    let Some(param_pointee) = self
                        .ptr_pointees
                        .get(&(callee.name.clone(), param_name.clone()))
                    else {
                        continue;
                    };
                    let (d, p) = (
                        self.resolve_known_type(declared),
                        self.resolve_known_type(param_pointee),
                    );
                    let widens = matches!(
                        (&d, &p),
                        (LlType::Vector(de, dl), LlType::Vector(pe, pl)) if de == pe && pl > dl
                    ) || padded_float3_rows_viewed_wide(&d, &p);
                    // A helper may view a whole local union as bytes; its allocation size stays.
                    let byte_view = whole_views
                        && self.is_pure_byte_image(&p)
                        && !self.type_contains_pointer(&d)
                        && (p == LlType::Int(8)
                            || self.is_local_alloca_reinterpret_candidate(&d, &p));
                    if widens || byte_view {
                        sources
                            .entry(root.clone())
                            .or_default()
                            .insert(param_pointee.clone());
                    }
                }
            }

            for (name, seen) in sources {
                let Some(original) = allocas.get(&name) else {
                    continue;
                };
                let original_resolved = self.resolve_known_type(original);
                let padded_rows_viewed_wide = seen.iter().any(|ty| {
                    padded_float3_rows_viewed_wide(&original_resolved, &self.resolve_known_type(ty))
                });
                if (padded_rows_viewed_wide
                    || seen
                        .iter()
                        .any(|ty| self.resolve_known_type(ty) == LlType::Int(8)))
                    && !self.type_contains_pointer(original)
                {
                    if let Some((size, _)) = self.native_memcpy_type_size_align(original) {
                        if let Ok(size) = u32::try_from(size) {
                            if size > 1 {
                                self.local_alloca_pointees.insert(
                                    (f.name.clone(), name.clone()),
                                    LlType::Array(Box::new(LlType::Int(8)), size),
                                );
                                continue;
                            }
                        }
                    }
                }
                if seen.contains(original)
                    && !self.is_pure_byte_image(&original_resolved)
                    && !self.type_contains_pointer(original)
                    && seen.iter().any(|ty| {
                        ty != original
                            && self.is_pure_byte_image(ty)
                            && self.is_local_alloca_reinterpret_candidate(original, ty)
                    })
                {
                    if let Some((size, _)) = self.native_memcpy_type_size_align(original) {
                        if let Ok(size) = u32::try_from(size) {
                            if size > 1 {
                                self.local_alloca_pointees.insert(
                                    (f.name.clone(), name.clone()),
                                    LlType::Array(Box::new(LlType::Int(8)), size),
                                );
                                continue;
                            }
                        }
                    }
                }
                let candidates = seen
                    .into_iter()
                    .filter(|ty| ty != original)
                    .filter(|ty| self.is_local_alloca_reinterpret_candidate(original, ty))
                    .collect::<Vec<_>>();
                if let [candidate] = candidates.as_slice() {
                    self.local_alloca_pointees
                        .insert((f.name.clone(), name), candidate.clone());
                } else if candidates.len() > 1 {
                    let byte_view = candidates.iter().find(|ty| {
                        matches!(self.resolve_known_type(ty),
                                 LlType::Array(elem, _) if *elem == LlType::Int(8))
                    });
                    if let Some(byte_view) = byte_view {
                        self.local_alloca_pointees
                            .insert((f.name.clone(), name), byte_view.clone());
                    }
                }
            }
        }
    }

    pub(in crate::native) fn local_allocas(&self, f: &LlFunction) -> HashMap<String, LlType> {
        let mut allocas = HashMap::default();
        for inst in f.carrier_insts() {
            let (Some(name), Some(ty)) = (&inst.result, &inst.alloca_ty()) else {
                continue;
            };
            allocas.insert(name.clone(), ty.clone());
        }
        allocas
    }

    pub(in crate::native) fn is_local_alloca_reinterpret_candidate(
        &self,
        original: &LlType,
        candidate: &LlType,
    ) -> bool {
        let original = self.resolve_known_type(original);
        let candidate = self.resolve_known_type(candidate);
        if self.type_contains_pointer(&original) || self.type_contains_pointer(&candidate) {
            return false;
        }
        if matches!(&original, LlType::Struct(fields) if fields.as_slice() == std::slice::from_ref(&candidate)) {
            return false;
        }
        let Some((original_size, _)) = self.native_memcpy_type_size_align(&original) else {
            return false;
        };
        let Some((candidate_size, _)) = self.native_memcpy_type_size_align(&candidate) else {
            return false;
        };
        original_size == candidate_size
    }

    pub(in crate::native) fn is_pure_byte_image(&self, ty: &LlType) -> bool {
        match self.resolve_known_type(ty) {
            LlType::Int(8) => true,
            LlType::Array(elem, len) => len > 0 && self.is_pure_byte_image(&elem),
            LlType::Struct(fields) => {
                !fields.is_empty() && fields.iter().all(|f| self.is_pure_byte_image(f))
            }
            _ => false,
        }
    }

    pub(crate) fn resolve_known_type(&self, ty: &LlType) -> LlType {
        match ty {
            LlType::Int(1) => LlType::Bool,
            LlType::Named(name) => self
                .types
                .get(name)
                .map(|ty| self.resolve_known_type(ty))
                .unwrap_or_else(|| ty.clone()),
            LlType::Vector(elem, 1) => self.resolve_known_type(elem),
            LlType::Vector(elem, lanes) => {
                LlType::Vector(Box::new(self.resolve_known_type(elem)), *lanes)
            }
            LlType::Array(elem, len) => {
                LlType::Array(Box::new(self.resolve_known_type(elem)), *len)
            }
            LlType::Struct(fields) => LlType::Struct(
                fields
                    .iter()
                    .map(|field| self.resolve_known_type(field))
                    .collect(),
            ),
            _ => ty.clone(),
        }
    }
}

fn padded_float3_rows_viewed_wide(original: &LlType, view: &LlType) -> bool {
    let LlType::Vector(view_elem, 4) = view else {
        return false;
    };
    let rows = match original {
        LlType::Array(elem, _) => elem.as_ref(),
        _ => return false,
    };
    matches!(rows, LlType::Vector(row_elem, 3) if row_elem == view_elem)
}

/// The pointers of `f` proven to address the whole of one alloca of `allocas`, each with that
/// alloca (from pull request 524 of the original project). An identity edge is an identity pointer
/// cast, a GEP with the single index zero, or a phi whose every incoming value is a local pointer.
/// The edges are walked as a graph, so a phi on a loop's backedge may cycle while every pointer it
/// reaches still names the same alloca. A pointer is a whole-object alias when everything it
/// reaches through those edges is an alloca or another identity edge, exactly one alloca is
/// reached, and every pointer on the way also has a path to an alloca: that last rule refuses a
/// rooted phi arm merged with an independent cycle that no alloca feeds.
fn whole_object_aliases(
    f: &LlFunction,
    allocas: &HashMap<String, LlType>,
) -> HashMap<String, String> {
    let mut whole_edges: HashMap<String, Vec<String>> = HashMap::default();
    for inst in f.carrier_insts() {
        if let Some((res, base)) = inst.identity_ptr_bitcast() {
            whole_edges.insert(res.to_string(), vec![base.to_string()]);
            continue;
        }
        let Some(res) = &inst.result else {
            continue;
        };
        if let Some(gep) = &inst.gep() {
            let zero_offset = matches!(
                gep.indices.as_slice(),
                [TypedValue {
                    value: LlValue::Int(0)
                        | LlValue::SignedInt(0)
                        | LlValue::Hex(0)
                        | LlValue::Zero,
                    ..
                }]
            );
            if let (true, LlValue::Local(base)) = (zero_offset, &gep.base.value) {
                whole_edges.insert(res.clone(), vec![base.clone()]);
            }
            continue;
        }
        if let Some(incoming) = inst.phi_values() {
            let edges: Option<Vec<String>> = incoming
                .map(|value| match value {
                    LlValue::Local(name) => Some(name.clone()),
                    _ => None,
                })
                .collect();
            if let Some(edges) = edges.filter(|edges| !edges.is_empty()) {
                whole_edges.insert(res.clone(), edges);
            }
        }
    }
    let mut parents: HashMap<String, Vec<String>> = HashMap::default();
    for (alias, edges) in &whole_edges {
        for base in edges {
            parents.entry(base.clone()).or_default().push(alias.clone());
        }
    }
    let mut rooted = HashSet::default();
    let mut pending: Vec<String> = allocas.keys().cloned().collect();
    while let Some(name) = pending.pop() {
        if !rooted.insert(name.clone()) {
            continue;
        }
        if let Some(aliases) = parents.get(&name) {
            pending.extend(aliases.iter().cloned());
        }
    }
    let mut aliases = HashMap::default();
    for alias in whole_edges.keys() {
        let mut pending = vec![alias.clone()];
        let mut visited = HashSet::default();
        let mut found = HashSet::default();
        let mut valid = true;
        while let Some(name) = pending.pop() {
            if !visited.insert(name.clone()) {
                continue;
            }
            if allocas.contains_key(&name) {
                found.insert(name);
            } else if let Some(edges) = whole_edges.get(&name) {
                pending.extend(edges.iter().cloned());
            } else {
                valid = false;
                break;
            }
        }
        if valid && found.len() == 1 && visited.iter().all(|name| rooted.contains(name)) {
            if let Some(root) = found.into_iter().next() {
                aliases.insert(alias.clone(), root);
            }
        }
    }
    aliases
}
