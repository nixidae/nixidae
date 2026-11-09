# Are the images we build the ones kubeadm will go looking for?
# Cheap, and the alternative is finding out from an
# ImagePullBackOff twenty minutes into the cluster test.
{ k8sImages, ... }:
k8sImages.check
