/* Which SMCCC function IDs does Hypervisor.framework hand to user space? */
#include <Hypervisor/Hypervisor.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>

#define GUEST_PA 0x10000ULL

static const uint64_t fids[] = {
    0x84000000, /* PSCI_VERSION */
    0x80000000, /* SMCCC_VERSION */
    0xc1000000, /* Apple CPU service: PAC SET_INITIAL_STATE */
    0xc1000001, /* Apple CPU service: PAC GET_DEFAULT_KEYS */
    0xc2000000, /* SiP service */
    0xc6000000, /* vendor hypervisor service */
};

int main(void) {
    hv_return_t r = hv_vm_create(NULL);
    if (r) { printf("vm_create 0x%x\n", r); return 1; }
    void *mem = mmap(NULL, 0x4000, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    uint32_t *code = mem;
    code[0] = 0xd4000002; /* hvc #0 */
    code[1] = 0xd4200000; /* brk #0 */
    r = hv_vm_map(mem, GUEST_PA, 0x4000, HV_MEMORY_READ | HV_MEMORY_EXEC);
    if (r) { printf("vm_map 0x%x\n", r); return 1; }

    for (size_t i = 0; i < sizeof fids / sizeof fids[0]; i++) {
        hv_vcpu_t vcpu;
        hv_vcpu_exit_t *exit;
        hv_vcpu_create(&vcpu, &exit, NULL);
        hv_vcpu_set_reg(vcpu, HV_REG_PC, GUEST_PA);
        hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5); /* EL1h, interrupts masked */
        hv_vcpu_set_reg(vcpu, HV_REG_X0, fids[i]);
        hv_vcpu_set_trap_debug_exceptions(vcpu, true);
        hv_vcpu_run(vcpu);
        uint64_t x0, pc;
        hv_vcpu_get_reg(vcpu, HV_REG_X0, &x0);
        hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
        uint32_t ec = (uint32_t)(exit->exception.syndrome >> 26) & 0x3f;
        printf("fid=0x%08llx exit_reason=%u ec=0x%02x pc=0x%llx x0=0x%llx -> %s\n",
               (unsigned long long)fids[i], exit->reason, ec, (unsigned long long)pc,
               (unsigned long long)x0, ec == 0x16 ? "user space sees HVC" : "kernel answered");
        hv_vcpu_destroy(vcpu);
    }
    return 0;
}
