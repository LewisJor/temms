# Pinned small-workload inputs

These twenty JPEGs total 654,484 bytes and support offline preparation of the
small workload. They are unchanged copies of the Hugging Face dataset-viewer JPEG
representation used when the workload was frozen; that upstream representation
may have been re-encoded from the original dataset images. The unchanged
[workload manifest](../workload.json) retains each original image ID, source row,
label, byte count and SHA-256. Preparation checks those bytes before copying them
into the chosen asset directory. Its `retrieval` field documents the original
acquisition; current small-workload preparation copies this frozen bundle.

Source: [Oxford-IIIT Pet Dataset](https://www.robots.ox.ac.uk/~vgg/data/pets/),
by Omkar M. Parkhi, Andrea Vedaldi, Andrew Zisserman and C. V. Jawahar,
*Cats and Dogs*, CVPR 2012. See the
[pinned Hugging Face dataset card](https://huggingface.co/datasets/timm/oxford-iiit-pet/blob/089695c834a7deb60505b7cc506672db1c31a6aa/README.md).
The dataset is distributed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Original image copyrights remain with their respective owners.

These fixtures are included in the source distribution, outside the TEMMS library
wheel and the edge qualification Docker build context. The separate validation
workload continues to use its frozen metadata and verified remote preparation.
