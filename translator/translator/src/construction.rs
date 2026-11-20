use crate::fxhash::HashMap;
use crate::{
    finish_module, meta, passes, stage_buffer_layouts, tools, FinishConstruction,
    FinishFailureKind, FinishedModule,
};
use std::path::Path;

pub(crate) struct ConstructionCtx<'a> {
    pub(crate) san_ll: &'a str,
    pub(crate) stage: passes::Stage,
    pub(crate) frag: Option<&'a meta::FragMeta>,
    pub(crate) vert: Option<&'a meta::VertMeta>,
    pub(crate) kern: Option<&'a meta::KernMeta>,
    pub(crate) entry_name: Option<&'a str>,
    pub(crate) tmp: &'a Path,
    pub(crate) options: passes::TransformOptions,
    pub(crate) air_data_layout: Option<crate::layout::AirDataLayout>,
    ordinary_plan_rejections: std::cell::RefCell<crate::fxhash::HashSet<String>>,
    ownership_plan_rejections: std::cell::RefCell<crate::fxhash::HashSet<String>>,
    post_lowering_cfg_construction: std::cell::Cell<bool>,
    primary_cfg_construction_failure: std::cell::Cell<bool>,
    primary_raw_buffer_construction_failure: std::cell::Cell<bool>,
}

impl<'a> ConstructionCtx<'a> {
    fn buffer_layouts(&self) -> Option<&'a HashMap<u32, meta::AirType>> {
        stage_buffer_layouts(self.stage, self.frag, self.vert, self.kern)
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        san_ll: &'a str,
        stage: passes::Stage,
        frag: Option<&'a meta::FragMeta>,
        vert: Option<&'a meta::VertMeta>,
        kern: Option<&'a meta::KernMeta>,
        entry_name: Option<&'a str>,
        tmp: &'a Path,
        options: passes::TransformOptions,
        air_data_layout: Option<crate::layout::AirDataLayout>,
    ) -> Self {
        ConstructionCtx {
            san_ll,
            stage,
            frag,
            vert,
            kern,
            entry_name,
            tmp,
            options,
            air_data_layout,
            ordinary_plan_rejections: std::cell::RefCell::new(Default::default()),
            ownership_plan_rejections: std::cell::RefCell::new(Default::default()),
            post_lowering_cfg_construction: std::cell::Cell::new(false),
            primary_cfg_construction_failure: std::cell::Cell::new(false),
            primary_raw_buffer_construction_failure: std::cell::Cell::new(false),
        }
    }

    #[cfg(test)]
    pub(crate) fn finish(
        &self,
        emitted: crate::emit_sidecar::EmittedSpirv,
    ) -> Result<Vec<u8>, String> {
        self.finish_carrier(emitted).map(|finished| finished.bytes)
    }

    #[cfg(test)]
    fn finish_carrier(
        &self,
        emitted: crate::emit_sidecar::EmittedSpirv,
    ) -> Result<FinishedModule, String> {
        self.finish_carrier_with_construction(emitted, FinishConstruction::Plain)
    }

    fn finish_carrier_with_construction(
        &self,
        emitted: crate::emit_sidecar::EmittedSpirv,
        construction: FinishConstruction,
    ) -> Result<FinishedModule, String> {
        finish_module(
            emitted,
            self.stage,
            self.frag,
            self.vert,
            self.kern,
            self.entry_name,
            self.air_data_layout.as_ref(),
            self.options,
            construction,
        )
        .map_err(|failure| failure.error)
    }

    pub(crate) fn finish_primary_carrier(
        &self,
        emitted: crate::emit_sidecar::EmittedSpirv,
    ) -> Result<FinishedModule, String> {
        finish_module(
            emitted,
            self.stage,
            self.frag,
            self.vert,
            self.kern,
            self.entry_name,
            self.air_data_layout.as_ref(),
            self.options,
            FinishConstruction::Primary,
        )
        .map_err(|failure| {
            match failure.kind {
                FinishFailureKind::CfgConstruction => {
                    self.primary_cfg_construction_failure.set(true);
                }
                FinishFailureKind::RawBufferConstruction => {
                    self.primary_raw_buffer_construction_failure.set(true);
                }
                FinishFailureKind::Other => {}
            }
            failure.error
        })
    }

    pub(crate) fn remember_ordinary_plan_rejections(
        &self,
        emitted: &crate::emit_sidecar::EmittedSpirv,
    ) {
        self.ordinary_plan_rejections.borrow_mut().extend(
            emitted
                .sidecar
                .ordinary_plan_rejected_functions
                .iter()
                .cloned(),
        );
        self.ownership_plan_rejections.borrow_mut().extend(
            emitted
                .sidecar
                .ownership_plan_rejected_functions
                .iter()
                .cloned(),
        );
        self.post_lowering_cfg_construction.set(
            self.post_lowering_cfg_construction.get()
                || !emitted
                    .sidecar
                    .post_lowering_cfg_construction_functions
                    .is_empty(),
        );
    }

    pub(crate) fn remember_ordinary_plan_rejection_set(
        &self,
        rejected: &crate::fxhash::HashSet<String>,
    ) {
        self.ordinary_plan_rejections
            .borrow_mut()
            .extend(rejected.iter().cloned());
    }

    pub(crate) fn has_cfg_plan_rejections(&self) -> bool {
        !self.ordinary_plan_rejections.borrow().is_empty()
            || !self.ownership_plan_rejections.borrow().is_empty()
            || self.post_lowering_cfg_construction.get()
            || self.primary_cfg_construction_failure.get()
    }

    pub(crate) fn remember_raw_buffer_layout_rejection(&self, required: bool) {
        if required {
            self.primary_raw_buffer_construction_failure.set(true);
        }
    }

    pub(crate) fn needs_raw_construction(&self) -> bool {
        self.has_cfg_plan_rejections() || self.primary_raw_buffer_construction_failure.get()
    }

    pub(crate) fn construct_raw(&self) -> Result<FinishedModule, String> {
        if self.has_cfg_plan_rejections() {
            return self.construct_raw_relooper();
        }
        let ordinary_rejections = self.ordinary_plan_rejections.borrow();
        let ownership_rejections = self.ownership_plan_rejections.borrow();
        let mut emitted = tools::emit_vulkan_spirv_all_buffers_raw_with_sidecar(
            self.san_ll,
            self.tmp,
            self.kern,
            self.entry_name,
            self.buffer_layouts(),
            &ordinary_rejections,
            &ownership_rejections,
        )?;
        emitted.sidecar.construct_cross_binding_addresses = true;
        self.finish_carrier_with_construction(emitted, FinishConstruction::Primary)
    }

    /// The module again with every device buffer read through its address (the address table at binding 640): a
    /// pointer select between two buffer roots, carried by a loop's pointer phi (`p = (s & 1) ? b : a`,
    /// xlate.paths-walk), has no Logical-addressing form, since each root is its own descriptor, but an address phi
    /// and an address select carry it. Only a primary construction refused for that, inside the last-resort scope,
    /// comes here (cross_root_select_refusal), so a module that translates today keeps its translation.
    pub(crate) fn construct_device_addresses(&self) -> Result<FinishedModule, String> {
        let ordinary_rejections = self.ordinary_plan_rejections.borrow();
        let ownership_rejections = self.ownership_plan_rejections.borrow();
        let mut emitted = tools::emit_vulkan_spirv_all_buffers_raw_bda_with_sidecar(
            self.san_ll,
            self.tmp,
            self.kern,
            self.entry_name,
            self.buffer_layouts(),
            &ordinary_rejections,
            &ownership_rejections,
        )?;
        emitted.sidecar.construct_cross_binding_addresses = true;
        self.finish_carrier_with_construction(emitted, FinishConstruction::Primary)
    }

    pub(crate) fn construct_raw_relooper(&self) -> Result<FinishedModule, String> {
        let mut emitted = tools::emit_vulkan_spirv_all_buffers_raw_relooper_feed_with_sidecar(
            self.san_ll,
            self.tmp,
            self.kern,
            self.entry_name,
            self.buffer_layouts(),
        )?;
        emitted.sidecar.construct_cross_binding_addresses = true;
        self.finish_carrier_with_construction(emitted, FinishConstruction::RawRelooper)
    }

    pub(crate) fn remember_ownership_plan_rejection_set(
        &self,
        rejected: &crate::fxhash::HashSet<String>,
    ) {
        self.ownership_plan_rejections
            .borrow_mut()
            .extend(rejected.iter().cloned());
    }
}

/// owned_cfg.rs's two refusals of an OpSelect whose arms point into different buffer roots.
pub(crate) fn is_cross_root_select_refusal(error: &str) -> bool {
    error.contains("cannot reinterpret pointer for owned OpSelect type mismatch")
        || error.contains("cannot retain cross-root pointer OpSelect under Logical addressing")
}

/// Whether the device-address retry is on: NVMTL_XLATE_ADDR_RETRY=0 (read once) leaves a module refused for a cross-root
/// select refused, as before. A value whose first byte is 0 turns it off, the byte the plugin reads to add -ar0 to its
/// shader cache tag (nvmtl_spvcache_addr_retry_tag in plugin/nvmtl_spvcache_name.h), so a module translated with the
/// retry on is never served with it off.
pub(crate) fn device_address_retry_on() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| {
        !std::env::var_os("NVMTL_XLATE_ADDR_RETRY").is_some_and(|v| v.as_encoded_bytes().first() == Some(&b'0'))
    })
}

thread_local! {
    static LAST_RESORT: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Runs `f` with the device-address construction allowed for a module whose primary construction is refused for a
/// cross-root select. A caller that tries several option sets for one module (the plugin's wrapper: formatless images
/// then default ones, argument-buffer samplers then ordinary ones) enters this only for one more pass after every one
/// of those attempts failed, so an attempt that used to fail this way can never pre-empt a later one that succeeded.
/// Outside it the two refusals stay refusals, as before.
pub fn with_device_address_last_resort<R>(f: impl FnOnce() -> R) -> R {
    struct Restore(bool);
    impl Drop for Restore {
        fn drop(&mut self) {
            LAST_RESORT.with(|c| c.set(self.0));
        }
    }
    let _restore = Restore(LAST_RESORT.with(|c| c.replace(true)));
    f()
}

pub fn device_address_last_resort_active() -> bool {
    LAST_RESORT.with(|c| c.get())
}

/// The primary construction's refusal `error` is retried with device addresses: only inside
/// with_device_address_last_resort, only for the two cross-root refusals and only while the retry is on.
pub(crate) fn cross_root_select_refusal(error: &str) -> bool {
    device_address_last_resort_active() && device_address_last_resort_applies(error)
}

/// Whether a translation that ended with `error` has a last resort: the error holds one of the two cross-root
/// refusals and the retry is on. A caller asks this before it runs its attempts again inside
/// with_device_address_last_resort, so a module refused for anything else is not translated twice.
pub fn device_address_last_resort_applies(error: &str) -> bool {
    is_cross_root_select_refusal(error) && device_address_retry_on()
}
