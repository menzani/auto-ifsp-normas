import fitz  # PyMuPDF

def ler_pdf(caminho_arquivo):
    try:
        doc = fitz.open(caminho_arquivo)
        texto_completo = ""
        
        for pagina in doc:
            texto_completo += pagina.get_text()
            
        return texto_completo
    except Exception as e:
        return f"Erro ao ler o PDF: {e}"

# Teste inicial
arquivo_teste = "peti.pdf" # Coloque o nome do seu PDF aqui
print(ler_pdf(arquivo_teste))