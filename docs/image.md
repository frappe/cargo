There are two ways to aquire a image.

Atlas will request for images from cargo once cargo is setup and live.

The app does not ship an image cargo will build the image on demand and upload it to it's object storage.
Subsequent requests for the image & tag will be direct pulls from object storage bucket.

The image build will be carried out by cargo itself — On spawn cargo will create a metadata bucket once garage is setup and up the bucket will be used by atlas and other internal services — for now just to pull pilot golden image.

Every release of pilot should be able to support multiple base images for example.
Version 0.1.0:
    - Pilot + a frappe site (v15)
    - Pilot + a frappe site (v16)
    - Pilot + a frappe site (develop)

