import base64
from io import BytesIO
from django.http import HttpResponse, JsonResponse # <-- JsonResponse add hua hai
from django.shortcuts import render
from PIL import Image, ImageDraw, ImageFont

def generate_tight_collage(images, margin=15, target_height=400, header_text="", footer_text=""):
    page_width, page_height = 1240, 1754 
    usable_width = page_width - (2 * margin)
    header_height = 100 if header_text else 0
    footer_height = 100 if footer_text else 0
    
    def create_new_page():
        page = Image.new('RGB', (page_width, page_height), color='white')
        draw = ImageDraw.Draw(page)
        try:
            try:
                font = ImageFont.truetype("arial.ttf", 40)
            except IOError:
                font = ImageFont.truetype("DejaVuSans.ttf", 40)
        except Exception:
            try:
                font = ImageFont.load_default(size=40)
            except:
                font = ImageFont.load_default()

        if header_text:
            bbox = draw.textbbox((0, 0), header_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (page_width - text_width) // 2
            y = (header_height - text_height) // 2
            draw.text((x, y), header_text, fill="black", font=font)
            
        if footer_text:
            bbox = draw.textbbox((0, 0), footer_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (page_width - text_width) // 2
            y = page_height - footer_height + (footer_height - text_height) // 2
            draw.text((x, y), footer_text, fill="black", font=font)
            
        return page

    pages = []
    current_page = create_new_page()
    current_y = margin + header_height 
    row_images = []
    row_aspect_sum = 0.0
    
    def place_row(row_imgs, aspect_sum, is_last_row=False):
        nonlocal current_page, current_y, pages
        if not row_imgs: return
        
        available_width = usable_width - (len(row_imgs) - 1) * margin
        if is_last_row and aspect_sum < (usable_width / target_height) * 0.7:
            row_height = target_height
        else:
            row_height = int(available_width / aspect_sum)
            
        max_allowed_height = page_height - (2 * margin) - header_height - footer_height
        row_height = min(row_height, max_allowed_height)
        
        if current_y + row_height > page_height - margin - footer_height:
            pages.append(current_page)
            current_page = create_new_page()
            current_y = margin + header_height
            
        current_x = margin
        for img in row_imgs:
            aspect = img.width / img.height
            new_width = int(row_height * aspect)
            resized_img = img.resize((new_width, row_height), Image.Resampling.LANCZOS)
            current_page.paste(resized_img, (current_x, current_y))
            current_x += new_width + margin
            
        current_y += row_height + margin

    for img in images:
        aspect = img.width / img.height
        row_images.append(img)
        row_aspect_sum += aspect
        available_width = usable_width - (len(row_images) - 1) * margin
        calculated_height = available_width / row_aspect_sum
        if calculated_height <= target_height:
            place_row(row_images, row_aspect_sum)
            row_images = []
            row_aspect_sum = 0.0
            
    if row_images:
        place_row(row_images, row_aspect_sum, is_last_row=True)
        
    pages.append(current_page)
    return pages

def upload_and_create_collage(request):
    if request.method == 'POST':
        
        # CONDITION 1: Download PDF Action
        if 'download_pdf' in request.POST:
            base64_pages = request.POST.getlist('pdf_pages')
            if base64_pages:
                pdf_images = []
                for b64_str in base64_pages:
                    img_data = base64.b64decode(b64_str)
                    img = Image.open(BytesIO(img_data))
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    pdf_images.append(img)
                
                if pdf_images:
                    buffer = BytesIO()
                    if len(pdf_images) > 1:
                        pdf_images[0].save(buffer, format='PDF', save_all=True, append_images=pdf_images[1:], resolution=100.0)
                    else:
                        pdf_images[0].save(buffer, format='PDF', resolution=100.0)
                    
                    pdf_data = buffer.getvalue()
                    buffer.close()
                    
                    response = HttpResponse(pdf_data, content_type='application/pdf')
                    response['Content-Disposition'] = 'attachment; filename="my_pro_collage.pdf"'
                    return response

        # CONDITION 2: Generate Preview
        uploaded_files = request.FILES.getlist('images')
        header_text = request.POST.get('header_text', '').strip()
        footer_text = request.POST.get('footer_text', '').strip()
        
        if uploaded_files:
            pil_images = []
            for file in uploaded_files:
                try:
                    img = Image.open(file)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    pil_images.append(img)
                except Exception as e:
                    continue
            
            if pil_images:
                pages = generate_tight_collage(pil_images, margin=15, target_height=400, header_text=header_text, footer_text=footer_text)
                
                pages_base64 = []
                for page in pages:
                    buffered = BytesIO()
                    page.save(buffered, format="JPEG", quality=80) 
                    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                    pages_base64.append(img_str)
                
                # NAYA CODE: Check agar request AJAX se hai, toh sirf JSON bhejo
                if request.POST.get('is_ajax') == '1':
                    return JsonResponse({'status': 'success', 'pages_base64': pages_base64})
                    
                return render(request, 'collage_app/upload.html', {'pages_base64': pages_base64})
                
    return render(request, 'collage_app/upload.html')