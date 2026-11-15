use crate::fxhash::{HashMap, HashSet};
use crate::spirv_module::{load_bytes, Module};
use crate::spirv_module::{Instruction, Operand};
use spirv::{Decoration, Op, StorageClass, Word};

mod bounded_work;
mod buffers;
mod byte_image;
mod control_flow;
mod interface;
mod intrinsics;
mod misc;
mod textures;
mod threadgroup;
mod types;

// A guarded load can live once in a shared function and be called at several
// sites. Recognize its returned load and null guard instead of treating every
// call as a lost load or counting its definition as a single execution.
fn guarded_load_argument(module: &Module, call: &Instruction) -> Option<Word> {
    if call.class.opcode != Op::FunctionCall || call.operands.len() != 3 {
        return None;
    }
    let Operand::IdRef(callee) = call.operands[0] else {
        return None;
    };
    let function = module
        .functions
        .iter()
        .find(|f| f.def.as_ref().and_then(|d| d.result_id) == Some(callee))?;
    let base = function.parameters.first()?.result_id?;
    let address = function.parameters.get(1)?.result_id?;
    let instructions = function
        .blocks
        .iter()
        .flat_map(|b| &b.instructions)
        .collect::<Vec<_>>();
    let definitions = instructions
        .iter()
        .filter_map(|i| Some((i.result_id?, *i)))
        .collect::<HashMap<_, _>>();
    let zero = |id: Word| {
        module.types_global_values.iter().any(|i| {
            i.result_id == Some(id)
                && (i.class.opcode == Op::ConstantNull
                    || (i.class.opcode == Op::Constant
                        && i.operands.iter().all(|v| {
                            matches!(v, Operand::LiteralBit32(0) | Operand::LiteralBit64(0))
                        })))
        })
    };
    let nonnull = instructions
        .iter()
        .find_map(|i| match i.operands.as_slice() {
            [Operand::IdRef(a), Operand::IdRef(b)]
                if i.class.opcode == Op::INotEqual && *a == base && zero(*b) =>
            {
                i.result_id
            }
            _ => None,
        })?;
    let load_block = instructions
        .iter()
        .find_map(|i| match i.operands.as_slice() {
            [Operand::IdRef(condition), Operand::IdRef(yes), _]
                if i.class.opcode == Op::BranchConditional && *condition == nonnull =>
            {
                Some(*yes)
            }
            _ => None,
        })?;
    let loaded = function
        .blocks
        .iter()
        .find(|b| b.label.as_ref().and_then(|i| i.result_id) == Some(load_block))?
        .instructions
        .iter()
        .find_map(|i| {
            if i.class.opcode != Op::Load || i.result_type != call.result_type {
                return None;
            }
            let Operand::IdRef(pointer) = i.operands.first()? else {
                return None;
            };
            let convert = definitions.get(pointer)?;
            (convert.class.opcode == Op::ConvertUToPtr
                && convert.operands.first() == Some(&Operand::IdRef(address)))
            .then_some(i.result_id?)
        })?;
    let returns_loaded = instructions.iter().any(|i| {
        if i.class.opcode != Op::ReturnValue {
            return false;
        }
        let Some(Operand::IdRef(value)) = i.operands.first() else {
            return false;
        };
        *value == loaded
            || definitions.get(value).is_some_and(|phi| {
                phi.class.opcode == Op::Phi
                    && phi
                        .operands
                        .chunks_exact(2)
                        .any(|arm| arm[0] == Operand::IdRef(loaded))
            })
    });
    if !returns_loaded {
        return None;
    }
    match call.operands.get(2)? {
        Operand::IdRef(argument) => Some(*argument),
        _ => None,
    }
}

fn effective_entry_load_count(module: &Module, types: &HashSet<Word>) -> usize {
    fn count(module: &Module, id: Word, types: &HashSet<Word>, path: &mut HashSet<Word>) -> usize {
        assert!(path.insert(id), "unexpected recursive shader helper");
        let function = module
            .functions
            .iter()
            .find(|f| f.def.as_ref().and_then(|d| d.result_id) == Some(id))
            .expect("defined helper");
        let total = function
            .blocks
            .iter()
            .flat_map(|b| &b.instructions)
            .map(|i| {
                if i.class.opcode == Op::Load && i.result_type.is_some_and(|t| types.contains(&t)) {
                    1
                } else if i.class.opcode == Op::FunctionCall {
                    match i.operands.first() {
                        Some(Operand::IdRef(callee)) => count(module, *callee, types, path),
                        _ => panic!("missing call target"),
                    }
                } else {
                    0
                }
            })
            .sum();
        path.remove(&id);
        total
    }
    let Operand::IdRef(entry) = module.entry_points[0].operands[1] else {
        panic!("entry point")
    };
    count(module, entry, types, &mut HashSet::default())
}

fn assert_nan_guarded_float_to_uint(module: &Module, lanes: usize) {
    let conversions = module
        .all_inst_iter()
        .filter(|i| i.class.opcode == Op::ConvertFToU)
        .collect::<Vec<_>>();
    assert_eq!(conversions.len(), lanes);
    for convert in conversions {
        let source = convert.operands.first().expect("conversion source");
        assert!(
            module.all_inst_iter().any(|select| {
                if select.class.opcode != Op::Select || select.result_type != convert.result_type {
                    return false;
                }
                let [Operand::IdRef(condition), Operand::IdRef(zero), Operand::IdRef(value)] =
                    select.operands.as_slice()
                else {
                    return false;
                };
                Some(*value) == convert.result_id
                    && module.types_global_values.iter().any(|i| {
                        i.result_id == Some(*zero)
                            && i.class.opcode == Op::ConstantNull
                            && i.result_type == convert.result_type
                    })
                    && module.all_inst_iter().any(|nan| {
                        nan.result_id == Some(*condition)
                            && nan.class.opcode == Op::IsNan
                            && nan.operands.first() == Some(source)
                    })
            }),
            "conversion {:?} lacks its NaN-to-zero selection",
            convert.result_id
        );
    }
}

fn descriptor_variable(module: &Module, set: u32, binding: u32) -> Option<Word> {
    module
        .types_global_values
        .iter()
        .filter(|i| i.class.opcode == Op::Variable)
        .find_map(|variable| {
            let id = variable.result_id?;
            let decorated = |decoration, value| {
                module.annotations.iter().any(|i| {
                    i.class.opcode == Op::Decorate
                        && i.operands.as_slice()
                            == [
                                Operand::IdRef(id),
                                Operand::Decoration(decoration),
                                Operand::LiteralBit32(value),
                            ]
                })
            };
            (decorated(Decoration::DescriptorSet, set) && decorated(Decoration::Binding, binding))
                .then_some(id)
        })
}

fn assert_sampled_texture_binding(module: &Module, index: u32) {
    let direct = crate::reflect::TEXTURE_BINDING_BASE + index;
    if descriptor_variable(module, crate::reflect::RESOURCE_DESCRIPTOR_SET, direct).is_some() {
        return;
    }
    let heap = descriptor_variable(
        module,
        crate::reflect::BINDLESS_HEAP_SET,
        crate::reflect::BINDLESS_HEAP_BINDING,
    )
    .expect("sampled texture heap");
    let table = descriptor_variable(module, crate::reflect::RESOURCE_DESCRIPTOR_SET, 640)
        .expect("resource address table");
    let slot = 2 * crate::reflect::BUFFER_BINDING_RANGE.end + index;
    let constants = module
        .types_global_values
        .iter()
        .filter_map(|i| {
            (i.class.opcode == Op::Constant
                && i.operands.first() == Some(&Operand::LiteralBit32(slot)))
            .then_some(i.result_id?)
        })
        .collect::<HashSet<_>>();
    let slot_pointers = module
        .all_inst_iter()
        .filter_map(|i| {
            (matches!(i.class.opcode, Op::AccessChain | Op::InBoundsAccessChain)
                && i.operands.first() == Some(&Operand::IdRef(table))
                && matches!(i.operands.get(2), Some(Operand::IdRef(id)) if constants.contains(id)))
            .then_some(i.result_id?)
        })
        .collect::<HashSet<_>>();
    let slot_loads = module
        .all_inst_iter()
        .filter_map(|i| {
            (i.class.opcode == Op::Load
            && matches!(i.operands.first(), Some(Operand::IdRef(id)) if slot_pointers.contains(id)))
            .then_some(i.result_id?)
        })
        .collect::<HashSet<_>>();
    let indices = module
        .all_inst_iter()
        .filter_map(|i| {
            (i.class.opcode == Op::BitwiseAnd
            && matches!(i.operands.first(), Some(Operand::IdRef(id)) if slot_loads.contains(id)))
            .then_some(i.result_id?)
        })
        .collect::<HashSet<_>>();
    assert!(
        module.all_inst_iter().any(|i| {
            matches!(i.class.opcode, Op::AccessChain | Op::InBoundsAccessChain)
                && i.operands.first() == Some(&Operand::IdRef(heap))
                && matches!(i.operands.get(1), Some(Operand::IdRef(id)) if indices.contains(id))
        }),
        "texture {index} must index the sampled heap using its own address-table slot"
    );
}

fn asm_has_line(asm: &str, needle: &str) -> bool {
    asm.lines().any(|line| line.trim() == needle)
}

fn whole_workgroup_options() -> crate::passes::TransformOptions {
    crate::passes::TransformOptions {
        kernel_dispatch: Some(crate::reflect::KernelDispatch::Workgroups),
        ..crate::passes::TransformOptions::default()
    }
}

fn assert_no_pointer_bitcasts(spv: &[u8]) {
    let module = load_bytes(spv).expect("load spv");
    let pointer_types = module
        .types_global_values
        .iter()
        .filter_map(|inst| {
            (inst.class.opcode == Op::TypePointer)
                .then_some(inst.result_id)
                .flatten()
        })
        .collect::<HashSet<_>>();
    let pointer_bitcast = module.all_inst_iter().find(|inst| {
        inst.class.opcode == Op::Bitcast
            && inst
                .result_type
                .is_some_and(|ty| pointer_types.contains(&ty))
    });
    assert!(pointer_bitcast.is_none(), "{pointer_bitcast:?}");
}

fn uint32_type_id(asm: &str) -> String {
    asm.lines()
        .find(|line| line.trim_end().ends_with("OpTypeInt 32 0"))
        .and_then(|line| line.split_whitespace().next())
        .unwrap_or("%uint")
        .to_string()
}

fn interface_variable_at_location(
    module: &Module,
    storage: StorageClass,
    location: u32,
) -> Option<Word> {
    module.annotations.iter().find_map(|inst| {
        if inst.class.opcode != Op::Decorate {
            return None;
        }
        let [
            Operand::IdRef(id),
            Operand::Decoration(Decoration::Location),
            Operand::LiteralBit32(loc),
        ] = inst.operands.as_slice()
        else {
            return None;
        };
        if *loc != location || variable_storage_class(module, *id) != Some(storage) {
            return None;
        }
        Some(*id)
    })
}

fn variable_storage_class(module: &Module, var: Word) -> Option<StorageClass> {
    module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::Variable && inst.result_id == Some(var) {
            if let Some(Operand::StorageClass(storage)) = inst.operands.first() {
                return Some(*storage);
            }
        }
        None
    })
}

fn pointer_type_storage_class(module: &Module, ptr_ty: Word) -> Option<StorageClass> {
    module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::TypePointer && inst.result_id == Some(ptr_ty) {
            if let Some(Operand::StorageClass(storage)) = inst.operands.first() {
                return Some(*storage);
            }
        }
        None
    })
}

fn variable_pointee_type(module: &Module, var: Word) -> Option<Word> {
    let ptr_ty = module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::Variable && inst.result_id == Some(var) {
            inst.result_type
        } else {
            None
        }
    })?;
    module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::TypePointer && inst.result_id == Some(ptr_ty) {
            if let Some(Operand::IdRef(pointee)) = inst.operands.get(1) {
                return Some(*pointee);
            }
        }
        None
    })
}

fn assert_phi_operand_types_match(module: &Module, asm: &str) {
    let result_types = module
        .all_inst_iter()
        .filter_map(|inst| Some((inst.result_id?, inst.result_type?)))
        .collect::<HashMap<_, _>>();
    for inst in module.all_inst_iter() {
        if inst.class.opcode != Op::Phi {
            continue;
        }
        let Some(result_type) = inst.result_type else {
            continue;
        };
        for incoming in inst.operands.chunks(2) {
            let [Operand::IdRef(value), Operand::IdRef(_label)] = incoming else {
                continue;
            };
            assert_eq!(
                result_types.get(value),
                Some(&result_type),
                "phi {:?} incoming value {value} has mismatched type\n{asm}",
                inst.result_id
            );
        }
    }
}

fn is_unsigned_int_vector(module: &Module, ty: Word, bits: u32, lanes: u32) -> bool {
    let Some(elem) = module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::TypeVector
            && inst.result_id == Some(ty)
            && inst.operands.get(1) == Some(&Operand::LiteralBit32(lanes))
        {
            if let Some(Operand::IdRef(elem)) = inst.operands.first() {
                return Some(*elem);
            }
        }
        None
    }) else {
        return false;
    };
    module.types_global_values.iter().any(|inst| {
        inst.class.opcode == Op::TypeInt
            && inst.result_id == Some(elem)
            && inst.operands.first() == Some(&Operand::LiteralBit32(bits))
            && inst.operands.get(1) == Some(&Operand::LiteralBit32(0))
    })
}

fn is_float_vector(module: &Module, ty: Word, lanes: u32) -> bool {
    let Some(elem) = module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::TypeVector
            && inst.result_id == Some(ty)
            && inst.operands.get(1) == Some(&Operand::LiteralBit32(lanes))
        {
            if let Some(Operand::IdRef(elem)) = inst.operands.first() {
                return Some(*elem);
            }
        }
        None
    }) else {
        return false;
    };
    module.types_global_values.iter().any(|inst| {
        inst.class.opcode == Op::TypeFloat
            && inst.result_id == Some(elem)
            && inst.operands.first() == Some(&Operand::LiteralBit32(32))
    })
}

fn is_signed_i32_vector(module: &Module, ty: Word, lanes: u32) -> bool {
    let Some(elem) = module.types_global_values.iter().find_map(|inst| {
        if inst.class.opcode == Op::TypeVector
            && inst.result_id == Some(ty)
            && inst.operands.get(1) == Some(&Operand::LiteralBit32(lanes))
        {
            if let Some(Operand::IdRef(elem)) = inst.operands.first() {
                return Some(*elem);
            }
        }
        None
    }) else {
        return false;
    };
    module.types_global_values.iter().any(|inst| {
        inst.class.opcode == Op::TypeInt
            && inst.result_id == Some(elem)
            && inst.operands.first() == Some(&Operand::LiteralBit32(32))
            && inst.operands.get(1) == Some(&Operand::LiteralBit32(1))
    })
}
