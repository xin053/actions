# actions

自用 `GitHub Actions`

## 备注

1. `diskimage-builder` `action` 如果 `build-param`(`Param of build_images.sh`) 参数需要配置 `COMMON_ELEMENTS`, 双引号需要转义, 例如 `COMMON_ELEMENTS=\"cleanup-kernel-initrd cloud-init cloud-init-datasources growroot pypi dynamic-login disable-nouveau mye\"`
2. `build-param` 参数配置示例: `IRONIC_PARTITION_IMAGE=yes FEDORA_V2V=yes`
