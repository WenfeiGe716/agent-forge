# Realistic asset pilot

The files listed in this directory's `manifest.json` are downloaded from [Poly Haven](https://polyhaven.com/)
through its official files API. Poly Haven's donated assets are released under
[CC0 1.0](https://polyhaven.com/license), which permits reuse and redistribution,
including in commercial products. The website text, logos and trademarks are
not part of this pack and are not redistributed.

`manifest.json` records the exact API URL, resolution, upstream md5 and local
sha256 for every file. The application must serve these files locally and must
not fetch them at runtime.

`modern_arm_chair_01` is a lounge chair, **not a workstation chair**.
`sofa_02` (antique sofa) and `street_lamp_02` (wall-mounted lantern, not a street
light pole) are unselected candidates. They are not the modern office default.

The `office_task_chair/` and `people/` directories have their own manifests and
licenses. The Microsoft Rocketbox human is MIT, not Poly Haven CC0.
