import math
import base64
from io import BytesIO
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from PIL import Image, ImageDraw, ImageFont

def generate_tight_collage(images, margin=15, header_text="", footer_text=""):
    page_width, page_height = 1240, 1754 
    usable_width = page_width - (2 * margin)
    
    header_height = 80 if header_text else 0
    footer_height = 70 if footer_text else 0
    
    # --- MAGIC FORMULA (Auto-Scale to perfectly fill A4 pages) ---
    total_aspect = sum(img.width / img.height for img in images)
    if total_aspect == 0:
        total_aspect = 1 # Safety fallback
        
    usable_page_height = page_height - (2 * margin) - header_height - footer_height
    
    # Estimate kitne pages lagenge agar default size ho
    est_total_height = (400 ** 2) * total_aspect / usable_width
    num_pages = max(1, round(est_total_height / usable_page_height))
    
    # Naya optimal image size calculate karna jisse page perfect bhar jaye
    optimal_height = math.sqrt((num_pages * usable_page_height * usable_width) / total_aspect)
    
    # Size ko bohot chhota ya bohot bada hone se rokna
    target_height = max(250, min(int(optimal_height), 700)) 
    # ---------------------------------------------------------------

    def create_new_page():
        page = Image.new('RGB', (page_width, page_height), color='white')
        draw = ImageDraw.Draw(page)
        
        try:
            try: font = ImageFont.truetype("arial.ttf", 35) 
            except IOError: font = ImageFont.truetype("DejaVuSans.ttf", 35)
        except Exception:
            try: font = ImageFont.load_default(size=35)
            except: font = ImageFont.load_default()

        # 1. HEADER TEXT (Hamesha top par)
        if header_text:
            bbox_0 = draw.textbbox((0, 0), header_text, font=font)
            text_width = bbox_0[2] - bbox_0[0]
            text_height = bbox_0[3] - bbox_0[1]
            
            x = (page_width - text_width) // 2
            y = (header_height - text_height) // 2 - bbox_0[1]
            
            bbox_actual = draw.textbbox((x, y), header_text, font=font)
            padding_x, padding_top, padding_bottom = 5, 8, 5
            
            highlight_box = [
                bbox_actual[0] - padding_x, bbox_actual[1] - padding_top, 
                bbox_actual[2] + padding_x, bbox_actual[3] + padding_bottom
            ]
            draw.rectangle(highlight_box, fill="#ffeb3b") 
            draw.text((x, y), header_text, fill="black", font=font)
            
        # 2. FOOTER TEXT (Hamesha fix bottom par rahega!)
        if footer_text:
            bbox_0 = draw.textbbox((0, 0), footer_text, font=font)
            text_width = bbox_0[2] - bbox_0[0]
            text_height = bbox_0[3] - bbox_0[1]
            
            x = (page_width - text_width) // 2
            # Footer absolute bottom calculate kar raha hai
            y = page_height - footer_height + (footer_height - text_height) // 2 - bbox_0[1]
            
            bbox_actual = draw.textbbox((x, y), footer_text, font=font)
            padding_x, padding_top, padding_bottom = 5, 8, 5
            
            highlight_box = [
                bbox_actual[0] - padding_x, bbox_actual[1] - padding_top, 
                bbox_actual[2] + padding_x, bbox_actual[3] + padding_bottom
            ]
            draw.rectangle(highlight_box, fill="#f3f4f6") 
            draw.text((x, y), footer_text, fill="#374151", font=font)
            
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
        
        # Aakhri line agar chhoti hai, toh usko forcefully stretch nahi karenge
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
        
        # dynamic target_height ka use ho raha hai
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
                # Target height ab function khud nikalega, isliye usko yahan se pass nahi kiya gaya hai
                pages = generate_tight_collage(pil_images, margin=15, header_text=header_text, footer_text=footer_text)
                
                pages_base64 = []
                for page in pages:
                    buffered = BytesIO()
                    page.save(buffered, format="JPEG", quality=80) 
                    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
                    pages_base64.append(img_str)
                
                if request.POST.get('is_ajax') == '1':
                    return JsonResponse({'status': 'success', 'pages_base64': pages_base64})
                    
                return render(request, 'collage_app/upload.html', {'pages_base64': pages_base64})
                
    return render(request, 'collage_app/upload.html')