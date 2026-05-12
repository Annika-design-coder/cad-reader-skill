(vl-load-com)

(defun codex-json-escape (s / i ch out code)
  (setq out "")
  (if s
    (progn
      (setq i 1)
      (while (<= i (strlen s))
        (setq ch (substr s i 1))
        (setq code (ascii ch))
        (cond
          ((= ch "\\") (setq out (strcat out "\\\\")))
          ((= ch "\"") (setq out (strcat out "\\\"")))
          ((= code 10) (setq out (strcat out "\\n")))
          ((= code 13) (setq out (strcat out "\\r")))
          ((= code 9) (setq out (strcat out "\\t")))
          (T (setq out (strcat out ch)))
        )
        (setq i (1+ i))
      )
    )
  )
  out
)

(defun codex-q (s)
  (strcat "\"" (codex-json-escape (if s s "")) "\"")
)

(defun codex-pt (pt)
  (if pt
    (strcat "[" (rtos (car pt) 2 8) "," (rtos (cadr pt) 2 8) "," (rtos (if (caddr pt) (caddr pt) 0.0) 2 8) "]")
    "null"
  )
)

(defun codex-list-strings (items / out first)
  (setq out "[" first T)
  (foreach item items
    (if first (setq first nil) (setq out (strcat out ",")))
    (setq out (strcat out (codex-q item)))
  )
  (strcat out "]")
)

(defun codex-table-names (table-name / row items)
  (setq items '())
  (setq row (tblnext table-name T))
  (while row
    (setq items (cons (cdr (assoc 2 row)) items))
    (setq row (tblnext table-name))
  )
  (reverse items)
)

(defun codex-layout-names (/ result names)
  (setq result (vl-catch-all-apply '(lambda () (layoutlist))))
  (if (vl-catch-all-error-p result)
    (setq names '("Model"))
    (setq names (cons "Model" result))
  )
  names
)

(defun codex-clean-text (edata / txt more)
  (setq txt "")
  (foreach pair edata
    (if (or (= (car pair) 1) (= (car pair) 3))
      (setq txt (strcat txt (cdr pair)))
    )
  )
  txt
)

(defun codex-string-replace (s old new / pos)
  (if (not s) (setq s ""))
  (if (and old (> (strlen old) 0))
    (while (setq pos (vl-string-search old s))
      (setq s
        (strcat
          (substr s 1 pos)
          new
          (substr s (+ pos (strlen old) 1))
        )
      )
    )
  )
  s
)

(defun codex-plain-text (s)
  (if (not s) (setq s ""))
  (setq s (codex-string-replace s "\\P" " "))
  (setq s (codex-string-replace s "\\p" " "))
  (setq s (codex-string-replace s "\\~" " "))
  (setq s (codex-string-replace s "{" ""))
  (setq s (codex-string-replace s "}" ""))
  (setq s (codex-string-replace s "%%c" "Φ"))
  (setq s (codex-string-replace s "%%C" "Φ"))
  (setq s (codex-string-replace s "%%p" "±"))
  (setq s (codex-string-replace s "%%P" "±"))
  (setq s (codex-string-replace s "%%d" "°"))
  (setq s (codex-string-replace s "%%D" "°"))
  s
)

(defun codex-vla-text-string (obj / result)
  (if obj
    (progn
      (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj 'TextString))))
      (if (vl-catch-all-error-p result) "" result)
    )
    ""
  )
)

(defun codex-bool (v)
  (if v "true" "false")
)

(defun codex-safe-number (expr / result)
  (setq result (vl-catch-all-apply expr))
  (if (vl-catch-all-error-p result)
    "null"
    (rtos result 2 8)
  )
)

(defun codex-bbox-json (obj / minpt maxpt result minlst maxlst)
  (setq result (vl-catch-all-apply
    '(lambda ()
      (vla-GetBoundingBox obj 'minpt 'maxpt)
      (setq minlst (vlax-safearray->list minpt))
      (setq maxlst (vlax-safearray->list maxpt))
      (strcat "{\"min\":" (codex-pt minlst) ",\"max\":" (codex-pt maxlst) "}")
    )
  ))
  (if (vl-catch-all-error-p result) "null" result)
)

(defun codex-vla-string (obj prop / result)
  (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj prop))))
  (if (vl-catch-all-error-p result) "" result)
)

(defun codex-vla-number-json (obj prop / result)
  (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj prop))))
  (if (vl-catch-all-error-p result)
    "null"
    (rtos result 2 8)
  )
)

(defun codex-vla-bool (obj prop / result)
  (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj prop))))
  (if (vl-catch-all-error-p result)
    nil
    (if result T nil)
  )
)

(defun codex-vla-int (obj prop default / result)
  (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj prop))))
  (if (vl-catch-all-error-p result)
    default
    (fix result)
  )
)

(defun codex-vla-point-json (obj prop / result value)
  (if obj
    (progn
      (setq result (vl-catch-all-apply '(lambda () (vlax-get-property obj prop))))
      (if (vl-catch-all-error-p result)
        "null"
        (progn
          (setq value result)
          (if (= (type value) 'VARIANT) (setq value (vlax-variant-value value)))
          (if (= (type value) 'SAFEARRAY) (setq value (vlax-safearray->list value)))
          (if (listp value) (codex-pt value) "null")
        )
      )
    )
    "null"
  )
)

(defun codex-dxf-number-json (edata code / pair)
  (setq pair (assoc code edata))
  (if pair (rtos (cdr pair) 2 8) "null")
)

(defun codex-dxf-string (edata code / pair)
  (setq pair (assoc code edata))
  (if pair (vl-princ-to-string (cdr pair)) "")
)

(defun codex-vertex-list-json (edata / pts pair out first)
  (setq pts '())
  (foreach pair edata
    (if (= (car pair) 10)
      (setq pts (append pts (list (cdr pair))))
    )
  )
  (setq out "[" first T)
  (foreach pair pts
    (if first (setq first nil) (setq out (strcat out ",")))
    (setq out (strcat out (codex-pt pair)))
  )
  (strcat out "]")
)

(defun codex-dxf-values-json (edata codes max-count / out first pair count value)
  (setq out "[" first T count 0)
  (foreach pair edata
    (if (and (member (car pair) codes) (< count max-count))
      (progn
        (if first (setq first nil) (setq out (strcat out ",")))
        (setq value (vl-princ-to-string (cdr pair)))
        (setq out
          (strcat out
            "{\"code\":" (itoa (car pair))
            ",\"value\":" (codex-q value)
            "}"
          )
        )
        (setq count (1+ count))
      )
    )
  )
  (strcat out "]")
)

(defun codex-dxf-code-counts-json (edata / counts pair found out first)
  (setq counts '())
  (foreach pair edata
    (setq found (assoc (car pair) counts))
    (if found
      (setq counts (subst (cons (car pair) (1+ (cdr found))) found counts))
      (setq counts (cons (cons (car pair) 1) counts))
    )
  )
  (setq out "{" first T)
  (foreach pair counts
    (if first (setq first nil) (setq out (strcat out ",")))
    (setq out (strcat out (codex-q (itoa (car pair))) ":" (itoa (cdr pair))))
  )
  (strcat out "}")
)

(defun codex-geometry-json (edata typ / p1 p2 center radius start end vertices)
  (cond
    ((= typ "LINE")
      (setq p1 (cdr (assoc 10 edata)))
      (setq p2 (cdr (assoc 11 edata)))
      (strcat "{\"type\":\"line\",\"points\":[" (codex-pt p1) "," (codex-pt p2) "]}")
    )
    ((member typ '("LWPOLYLINE" "POLYLINE"))
      (setq vertices (codex-vertex-list-json edata))
      (strcat "{\"type\":\"polyline\",\"points\":" vertices "}")
    )
    ((= typ "CIRCLE")
      (setq center (cdr (assoc 10 edata)))
      (setq radius (cdr (assoc 40 edata)))
      (strcat "{\"type\":\"circle\",\"center\":" (codex-pt center) ",\"radius\":" (rtos (if radius radius 0.0) 2 8) "}")
    )
    ((= typ "ARC")
      (setq center (cdr (assoc 10 edata)))
      (setq radius (cdr (assoc 40 edata)))
      (setq start (cdr (assoc 50 edata)))
      (setq end (cdr (assoc 51 edata)))
      (strcat "{\"type\":\"arc\",\"center\":" (codex-pt center) ",\"radius\":" (rtos (if radius radius 0.0) 2 8) ",\"start\":" (rtos (if start start 0.0) 2 8) ",\"end\":" (rtos (if end end 0.0) 2 8) "}")
    )
    (T "null")
  )
)

(defun codex-points-by-codes-json (edata codes / pts pair out first)
  (setq pts '())
  (foreach pair edata
    (if (member (car pair) codes)
      (setq pts (append pts (list (cdr pair))))
    )
  )
  (setq out "[" first T)
  (foreach pair pts
    (if first (setq first nil) (setq out (strcat out ",")))
    (setq out (strcat out (codex-pt pair)))
  )
  (strcat out "]")
)

(defun codex-dimtype-name (dimtype / base)
  (setq base (logand dimtype 7))
  (cond
    ((= base 0) "rotated_linear")
    ((= base 1) "aligned")
    ((= base 2) "angular")
    ((= base 3) "diameter")
    ((= base 4) "radius")
    ((= base 5) "angular_3point")
    ((= base 6) "ordinate")
    (T "unknown")
  )
)

(defun codex-text-info-json (obj edata typ text / raw vla plain style height rotation width oblique align hjust vjust generation)
  (if (member typ '("TEXT" "MTEXT" "ATTRIB" "ATTDEF"))
    (progn
      (setq raw (codex-clean-text edata))
      (setq vla (codex-vla-text-string obj))
      (setq plain (codex-plain-text (if (/= vla "") vla raw)))
      (setq style (if obj (codex-vla-string obj 'StyleName) (codex-dxf-string edata 7)))
      (setq height (codex-dxf-number-json edata 40))
      (setq rotation (codex-dxf-number-json edata 50))
      (setq width (codex-dxf-number-json edata 41))
      (setq oblique (codex-dxf-number-json edata 51))
      (setq align (codex-dxf-string edata 71))
      (setq hjust (codex-dxf-string edata 72))
      (setq vjust (codex-dxf-string edata 73))
      (setq generation (codex-dxf-string edata 71))
      (strcat
        "{"
        "\"raw_text\":" (codex-q raw)
        ",\"vla_text\":" (codex-q vla)
        ",\"plain_text\":" (codex-q plain)
        ",\"style_name\":" (codex-q style)
        ",\"height\":" height
        ",\"rotation\":" rotation
        ",\"width_factor\":" width
        ",\"oblique\":" oblique
        ",\"attachment\":" (codex-q align)
        ",\"horizontal_justification\":" (codex-q hjust)
        ",\"vertical_justification\":" (codex-q vjust)
        ",\"generation_flags\":" (codex-q generation)
        "}"
      )
    )
    "null"
  )
)

(defun codex-attribute-json (att / tag txt pos bbox invisible constant height)
  (setq tag (codex-vla-string att 'TagString))
  (setq txt (codex-vla-string att 'TextString))
  (setq pos (codex-vla-point-json att 'InsertionPoint))
  (setq bbox (codex-bbox-json att))
  (setq invisible (codex-vla-bool att 'Invisible))
  (setq constant (codex-vla-bool att 'Constant))
  (setq height (codex-vla-number-json att 'Height))
  (strcat
    "{"
    "\"tag\":" (codex-q tag)
    ",\"text\":" (codex-q txt)
    ",\"plain_text\":" (codex-q (codex-plain-text txt))
    ",\"position\":" pos
    ",\"height\":" height
    ",\"bbox\":" bbox
    ",\"invisible\":" (codex-bool invisible)
    ",\"constant\":" (codex-bool constant)
    "}"
  )
)

(defun codex-attributes-json (obj typ / has result value attrs out first att)
  (if (and (= typ "INSERT") obj)
    (progn
      (setq has (codex-vla-bool obj 'HasAttributes))
      (if has
        (progn
          (setq result (vl-catch-all-apply '(lambda () (vlax-invoke obj 'GetAttributes))))
          (if (vl-catch-all-error-p result)
            "[]"
            (progn
              (setq value result)
              (if (= (type value) 'VARIANT) (setq value (vlax-variant-value value)))
              (if (= (type value) 'SAFEARRAY) (setq attrs (vlax-safearray->list value)) (setq attrs value))
              (setq out "[" first T)
              (foreach att attrs
                (if first (setq first nil) (setq out (strcat out ",")))
                (setq out (strcat out (codex-attribute-json att)))
              )
              (strcat out "]")
            )
          )
        )
        "[]"
      )
    )
    "[]"
  )
)

(defun codex-proxy-json (obj edata typ vlatyp bbox / isproxy class-markers apps strings handles codecounts binary-count has-bbox)
  (setq isproxy (or (= typ "ACAD_PROXY_ENTITY") (wcmatch (strcase vlatyp) "*PROXY*")))
  (if isproxy
    (progn
      (setq class-markers (codex-dxf-values-json edata '(100) 20))
      (setq apps (codex-dxf-values-json edata '(1001) 20))
      (setq strings (codex-dxf-values-json edata '(1 2 3 6 7 8 1000 1002) 40))
      (setq handles (codex-dxf-values-json edata '(330 331 340 341 350 360) 30))
      (setq codecounts (codex-dxf-code-counts-json edata))
      (setq binary-count 0)
      (foreach pair edata
        (if (member (car pair) '(310 311 312 313 314 315 316 317 318 319))
          (setq binary-count (1+ binary-count))
        )
      )
      (setq has-bbox (and bbox (/= bbox "null")))
      (strcat
        "{"
        "\"kind\":\"proxy_entity\""
        ",\"diagnosis\":\"custom_or_missing_object_enabler\""
        ",\"has_bbox\":" (codex-bool has-bbox)
        ",\"bbox_available\":" (codex-bool has-bbox)
        ",\"class_markers\":" class-markers
        ",\"application_names\":" apps
        ",\"string_values\":" strings
        ",\"handle_references\":" handles
        ",\"binary_chunk_count\":" (itoa binary-count)
        ",\"dxf_code_counts\":" codecounts
        "}"
      )
    )
    "null"
  )
)

(defun codex-dimension-json (obj edata typ / dimtext override style dimtype dimtype-num dimtype-name points measurement textpos blockname angle horiz oblique defpt textpt ext1 ext2)
  (if (= typ "DIMENSION")
    (progn
      (setq dimtext (codex-dxf-string edata 1))
      (setq override (if obj (codex-vla-string obj 'TextOverride) ""))
      (setq style (if obj (codex-vla-string obj 'StyleName) (if (assoc 3 edata) (cdr (assoc 3 edata)) "")))
      (setq measurement (if obj (codex-vla-number-json obj 'Measurement) "null"))
      (if (= measurement "null") (setq measurement (codex-dxf-number-json edata 42)))
      (setq dimtype-num (if (assoc 70 edata) (cdr (assoc 70 edata)) 0))
      (setq dimtype (if (assoc 70 edata) (itoa dimtype-num) "null"))
      (setq dimtype-name (codex-dimtype-name dimtype-num))
      (setq points (codex-points-by-codes-json edata '(10 11 12 13 14 15 16)))
      (setq textpos (codex-vla-point-json obj 'TextPosition))
      (setq blockname (codex-dxf-string edata 2))
      (setq angle (codex-dxf-number-json edata 50))
      (setq horiz (codex-dxf-number-json edata 51))
      (setq oblique (codex-dxf-number-json edata 52))
      (setq defpt (if (assoc 10 edata) (codex-pt (cdr (assoc 10 edata))) "null"))
      (setq textpt (if (assoc 11 edata) (codex-pt (cdr (assoc 11 edata))) "null"))
      (setq ext1 (if (assoc 13 edata) (codex-pt (cdr (assoc 13 edata))) "null"))
      (setq ext2 (if (assoc 14 edata) (codex-pt (cdr (assoc 14 edata))) "null"))
      (strcat
        "{"
        "\"measurement\":" measurement
        ",\"display_text\":" (codex-q dimtext)
        ",\"plain_text\":" (codex-q (codex-plain-text (if (/= override "") override dimtext)))
        ",\"text_override\":" (codex-q override)
        ",\"style_name\":" (codex-q style)
        ",\"dimension_type\":" dimtype
        ",\"dimension_type_name\":" (codex-q dimtype-name)
        ",\"dimension_block_name\":" (codex-q blockname)
        ",\"definition_point\":" defpt
        ",\"text_midpoint\":" textpt
        ",\"text_position\":" textpos
        ",\"extension_line_1_point\":" ext1
        ",\"extension_line_2_point\":" ext2
        ",\"angle\":" angle
        ",\"horizontal_direction\":" horiz
        ",\"oblique_angle\":" oblique
        ",\"definition_points\":" points
        ",\"property_source\":" (codex-q (if obj "vla+dxf" "dxf"))
        "}"
      )
    )
    "null"
  )
)

(defun codex-table-cells-json (obj rows cols / r c out first txt result count maxcells)
  (setq out "[" first T r 0 count 0 maxcells 2000)
  (while (and (< r rows) (< count maxcells))
    (setq c 0)
    (while (and (< c cols) (< count maxcells))
      (setq result (vl-catch-all-apply '(lambda () (vla-GetText obj r c))))
      (setq txt (if (vl-catch-all-error-p result) "" result))
      (if (/= txt "")
        (progn
          (if first (setq first nil) (setq out (strcat out ",")))
          (setq out
            (strcat out
              "{\"row\":" (itoa r)
              ",\"column\":" (itoa c)
              ",\"text\":" (codex-q txt)
              "}"
            )
          )
        )
      )
      (setq c (1+ c))
      (setq count (1+ count))
    )
    (setq r (1+ r))
  )
  (strcat out "]")
)

(defun codex-table-json (obj typ / rows cols cells cellcount truncated)
  (if (= typ "ACAD_TABLE")
    (if obj
      (progn
        (setq rows (codex-vla-int obj 'Rows 0))
        (setq cols (codex-vla-int obj 'Columns 0))
        (setq cells (codex-table-cells-json obj rows cols))
        (setq cellcount (* rows cols))
        (setq truncated (> cellcount 2000))
        (strcat
          "{"
          "\"rows\":" (itoa rows)
          ",\"columns\":" (itoa cols)
          ",\"cell_count\":" (itoa cellcount)
          ",\"cells_truncated\":" (codex-bool truncated)
          ",\"cells\":" cells
          ",\"property_source\":\"vla\""
          "}"
        )
      )
      "{\"rows\":null,\"columns\":null,\"cell_count\":null,\"cells_truncated\":false,\"cells\":[],\"property_source\":\"unavailable\",\"read_warning\":\"ACAD_TABLE cell data requires VLA/Object Enabler support\"}"
    )
    "null"
  )
)

(defun codex-error-entity-json (edata message / typ layer handle)
  (setq typ (cdr (assoc 0 edata)))
  (setq layer (cdr (assoc 8 edata)))
  (setq handle (cdr (assoc 5 edata)))
  (strcat
    "{"
    "\"handle\":" (codex-q handle)
    ",\"object_name\":" (codex-q typ)
    ",\"vla_object_name\":\"\""
    ",\"layer\":" (codex-q layer)
    ",\"space\":\"model\""
    ",\"text\":\"\""
    ",\"position\":null"
    ",\"block_name\":\"\""
    ",\"closed\":false"
    ",\"length\":null"
    ",\"area\":null"
    ",\"bbox\":null"
    ",\"geometry\":null"
    ",\"text_info\":null"
    ",\"attributes\":[]"
    ",\"dimension\":null"
    ",\"table\":null"
    ",\"proxy\":null"
    ",\"read_error\":" (codex-q message)
    "}"
  )
)

(defun codex-entity-json (ename / edata obj obj-result typ vlatyp layer handle text rawtext vlatext textinfo attrs proxy ins block closed len area bbox geom dim table json)
  (setq edata (entget ename))
  (setq obj-result (vl-catch-all-apply '(lambda () (vlax-ename->vla-object ename))))
  (if (vl-catch-all-error-p obj-result)
    (setq obj nil)
    (setq obj obj-result)
  )
  (setq typ (cdr (assoc 0 edata)))
  (setq vlatyp (if obj (codex-vla-string obj 'ObjectName) ""))
  (setq layer (cdr (assoc 8 edata)))
  (setq handle (cdr (assoc 5 edata)))
  (setq text "")
  (if (member typ '("TEXT" "MTEXT" "ATTRIB" "ATTDEF"))
    (progn
      (setq rawtext (codex-clean-text edata))
      (setq vlatext (codex-vla-text-string obj))
      (setq text (codex-plain-text (if (/= vlatext "") vlatext rawtext)))
    )
  )
  (if (= typ "DIMENSION")
    (setq text (codex-plain-text (codex-dxf-string edata 1)))
  )
  (setq ins (cdr (assoc 10 edata)))
  (setq block (cdr (assoc 2 edata)))
  (setq closed nil)
  (if (and (member typ '("LWPOLYLINE" "POLYLINE")) (assoc 70 edata))
    (setq closed (= 1 (logand 1 (cdr (assoc 70 edata)))))
  )
  (setq len (if obj (codex-safe-number '(lambda () (vlax-curve-getDistAtParam obj (vlax-curve-getEndParam obj)))) "null"))
  (setq area "null")
  (if (and obj (member typ '("LWPOLYLINE" "POLYLINE" "CIRCLE" "HATCH" "REGION")) (vlax-property-available-p obj 'Area))
    (setq area (codex-safe-number '(lambda () (vla-get-Area obj))))
  )
  (setq bbox (if obj (codex-bbox-json obj) "null"))
  (setq geom (codex-geometry-json edata typ))
  (setq textinfo (codex-text-info-json obj edata typ text))
  (setq attrs (codex-attributes-json obj typ))
  (setq dim (codex-dimension-json obj edata typ))
  (setq table (codex-table-json obj typ))
  (setq proxy (codex-proxy-json obj edata typ vlatyp bbox))
  (setq json
    (strcat
      "{"
      "\"handle\":" (codex-q handle)
      ",\"object_name\":" (codex-q typ)
      ",\"vla_object_name\":" (codex-q vlatyp)
      ",\"layer\":" (codex-q layer)
      ",\"space\":\"model\""
      ",\"text\":" (codex-q text)
      ",\"position\":" (codex-pt ins)
      ",\"block_name\":" (codex-q block)
      ",\"closed\":" (codex-bool closed)
      ",\"length\":" len
      ",\"area\":" area
      ",\"bbox\":" bbox
      ",\"geometry\":" geom
      ",\"text_info\":" textinfo
      ",\"attributes\":" attrs
      ",\"dimension\":" dim
      ",\"table\":" table
      ",\"proxy\":" proxy
      "}"
    )
  )
  json
)

(defun codex-export-dwg-json (output limit / ss n i ent item item-result typ counts texts dims tables blockrefs entities first f layers blocks layouts json edata)
  (setq layers (codex-table-names "LAYER"))
  (setq blocks (codex-table-names "BLOCK"))
  (setq layouts (codex-layout-names))
  (setq counts '())
  (setq texts '())
  (setq dims '())
  (setq tables '())
  (setq blockrefs '())
  (setq entities '())
  (setq ss (ssget "_X" '((410 . "Model"))))
  (setq n (if ss (sslength ss) 0))
  (setq i 0)
  (while (and (< i n) (< i limit))
    (setq ent (ssname ss i))
    (setq edata (entget ent))
    (setq item-result (vl-catch-all-apply '(lambda () (codex-entity-json ent))))
    (if (vl-catch-all-error-p item-result)
      (setq item (codex-error-entity-json edata (vl-catch-all-error-message item-result)))
      (setq item item-result)
    )
    (setq typ (cdr (assoc 0 edata)))
    (if (assoc typ counts)
      (setq counts (subst (cons typ (1+ (cdr (assoc typ counts)))) (assoc typ counts) counts))
      (setq counts (cons (cons typ 1) counts))
    )
    (setq entities (cons item entities))
    (if (member typ '("TEXT" "MTEXT" "ATTRIB" "ATTDEF")) (setq texts (cons item texts)))
    (if (= typ "DIMENSION") (setq dims (cons item dims)))
    (if (= typ "ACAD_TABLE") (setq tables (cons item tables)))
    (if (= typ "INSERT") (setq blockrefs (cons item blockrefs)))
    (setq i (1+ i))
  )
  (setq f (open output "w"))
  (write-line "{" f)
  (write-line "\"status\":\"ok\"," f)
  (write-line (strcat "\"file_path\":" (codex-q (getvar "DWGPREFIX")) ",") f)
  (write-line "\"reader\":{\"name\":\"remote-bricscad-native\",\"version\":\"BricsCAD AutoLISP\",\"status\":\"ok\"}," f)
  (write-line (strcat "\"document\":{\"name\":" (codex-q (getvar "DWGNAME")) ",\"units\":null,\"ins_units\":null,\"model_space_entity_counts\":{") f)
  (setq first T)
  (foreach pair counts
    (if first (setq first nil) (write-line "," f))
    (princ (strcat (codex-q (car pair)) ":" (itoa (cdr pair))) f)
  )
  (write-line "},\"paper_space_entity_counts\":{}}," f)
  (write-line (strcat "\"layouts\":" (codex-list-strings layouts) ",") f)
  (write-line (strcat "\"layers\":" (codex-list-strings layers) ",") f)
  (write-line (strcat "\"blocks\":" (codex-list-strings blocks) ",") f)
  (write-line "\"xrefs\":[]," f)
  (write-line (strcat "\"texts\":[" (apply 'strcat (mapcar '(lambda (x) (strcat x ",")) (reverse texts))) "null],") f)
  (write-line (strcat "\"dimensions\":[" (apply 'strcat (mapcar '(lambda (x) (strcat x ",")) (reverse dims))) "null],") f)
  (write-line (strcat "\"tables\":[" (apply 'strcat (mapcar '(lambda (x) (strcat x ",")) (reverse tables))) "null],") f)
  (write-line (strcat "\"entities\":[" (apply 'strcat (mapcar '(lambda (x) (strcat x ",")) (reverse entities))) "null],") f)
  (write-line (strcat "\"block_references\":[" (apply 'strcat (mapcar '(lambda (x) (strcat x ",")) (reverse blockrefs))) "null],") f)
  (write-line (strcat "\"extraction\":{\"entity_limit\":" (itoa limit) ",\"processed_count\":" (itoa i) ",\"total_model_entity_count\":" (itoa n) ",\"is_truncated\":" (codex-bool (< i n)) "},") f)
  (write-line "\"errors\":[]" f)
  (write-line "}" f)
  (close f)
  (princ)
)

(defun codex-export-window-png (output minx miny maxx maxy / p1 p2 ss old-filedia old-cmddia)
  (setq old-filedia (getvar "FILEDIA"))
  (setq old-cmddia (getvar "CMDDIA"))
  (setvar "FILEDIA" 0)
  (setvar "CMDDIA" 0)
  (vl-catch-all-apply '(lambda () (setvar "TILEMODE" 1)))
  (setq p1 (list minx miny 0.0))
  (setq p2 (list maxx maxy 0.0))
  (vl-catch-all-apply '(lambda () (command "_.ZOOM" "_W" p1 p2)))
  (setq ss (ssget "_C" p1 p2))
  (if ss
    (vl-catch-all-apply '(lambda () (command "_.PNGOUT" output ss "")))
    (vl-catch-all-apply '(lambda () (command "_.PNGOUT" output "_C" p1 p2 "")))
  )
  (setvar "FILEDIA" old-filedia)
  (setvar "CMDDIA" old-cmddia)
  (princ)
)
