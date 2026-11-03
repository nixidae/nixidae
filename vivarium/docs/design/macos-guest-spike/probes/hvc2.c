/* Does an Apple CPU service call reach user space in another instruction form? */
#include <Hypervisor/Hypervisor.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>

#define GUEST_PA 0x10000ULL

static const struct {
    const char *name;
    uint32_t insn;
} forms[] = {
    {"hvc #0", 0xd4000002},
    {"hvc #1", 0xd4000002 | (1u << 5)},
    {"hvc #0xffff", 0xd4000002 | (0xffffu << 5)},
    {"smc #0", 0xd4000003},
    {"smc #1", 0xd4000003 | (1u << 5)},
};

int main(void) {
    if (hv_vm_create(NULL)) return 1;
    uint32_t *code = mmap(NULL, 0x4000, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (hv_vm_map(code, GUEST_PA, 0x4000, HV_MEMORY_READ | HV_MEMORY_EXEC)) return 1;
    code[1] = 0xd4200000; /* brk #0 */

    for (size_t i = 0; i < sizeof forms / sizeof forms[0]; i++) {
        code[0] = forms[i].insn;
        hv_vcpu_t vcpu;
        hv_vcpu_exit_t *exit;
        hv_vcpu_create(&vcpu, &exit, NULL);
        hv_vcpu_set_reg(vcpu, HV_REG_PC, GUEST_PA);
        hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5);
        hv_vcpu_set_reg(vcpu, HV_REG_X0, 0xc1000000);
        hv_vcpu_set_trap_debug_exceptions(vcpu, true);
        hv_vcpu_run(vcpu);
        uint64_t x0, pc;
        hv_vcpu_get_reg(vcpu, HV_REG_X0, &x0);
        hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
        uint32_t ec = (uint32_t)(exit->exception.syndrome >> 26) & 0x3f;
        uint32_t iss = (uint32_t)exit->exception.syndrome & 0xffff;
        printf("%-12s x0=0xc1000000 -> ec=0x%02x iss=0x%04x pc=0x%llx x0=0x%llx\n", forms[i].name, ec, iss,
               (unsigned long long)pc, (unsigned long long)x0);
        hv_vcpu_destroy(vcpu);
    }
    return 0;
}
