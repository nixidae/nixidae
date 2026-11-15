#include <Hypervisor/Hypervisor.h>
#include <stdio.h>
#include <stdint.h>
extern hv_return_t _hv_vm_config_set_isa(hv_vm_config_t, uint32_t);
extern hv_return_t _hv_vm_config_get_isa(hv_vm_config_t, uint32_t *);
int main(void) {
    hv_vm_config_t c = hv_vm_config_create();
    uint32_t isa = 99;
    hv_return_t r = _hv_vm_config_get_isa(c, &isa);
    printf("default get_isa: ret=0x%x isa=%u\n", r, isa);
    for (uint32_t v = 0; v < 6; v++) {
        hv_vm_config_t cc = hv_vm_config_create();
        hv_return_t s = _hv_vm_config_set_isa(cc, v);
        hv_return_t m = s == HV_SUCCESS ? hv_vm_create(cc) : -1;
        printf("isa=%u set=0x%x vm_create=0x%x\n", v, s, m);
        if (m == HV_SUCCESS) hv_vm_destroy();
    }
    return 0;
}
