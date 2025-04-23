# chunking.py

import os
import re
from pathlib import Path
from typing import List, Dict
from dataclasses import dataclass
from config import VectorizationConfig

@dataclass
class CodeChunk:
    text: str
    file_path: str
    start_line: int
    end_line: int
    chunk_type: str  # 'function', 'class', 'block', etc.
    language: str
    metadata: Dict

class LanguageParser:
    PATTERNS = {
        'python': {
            'function': r'(async\s+)?def\s+\w+\s*\([^)]*\)\s*(?:->[^:]+)?:',
            'class': r'class\s+\w+(?:\([^)]*\))?\s*:',
            'docstring': r'(?:\'\'\'[\s\S]*?\'\'\'|"""[\s\S]*?""")',
        },
        'javascript': {
            'function': r'(?:async\s+)?(?:function\s+\w+|\w+\s*=\s*(?:async\s+)?\([^)]*\)\s*=>)',
            'class': r'class\s+\w+(?:\s+extends\s+\w+)?\s*\{',
            'jsx_component': r'(?:export\s+)?(?:default\s+)?function\s+[A-Z]\w*\s*\([^)]*\)',
        },
        # Add more languages as needed
    }
    
    @classmethod
    def detect_language(cls, file_path: str) -> str:
        ext_map = {
            '.py': 'python',
            '.js': 'javascript',
            '.jsx': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'typescript',
            # Add more mappings
        }
        return ext_map.get(Path(file_path).suffix.lower(), 'unknown')

class SmartChunker:
    def __init__(self, config: VectorizationConfig):
        self.config = config
        
    
    def chunk_file(self, file_path: str, content: str) -> List[CodeChunk]:
        language = LanguageParser.detect_language(file_path)
        if language == 'unknown':
            return self._chunk_by_size(file_path, content, language)  # Pass the 'language' here

        patterns = LanguageParser.PATTERNS.get(language, {})
        chunks = []

        # First pass: Find all semantic boundaries
        boundaries = []
        for chunk_type, pattern in patterns.items():
            for match in re.finditer(pattern, content, re.MULTILINE):
                boundaries.append({
                    'start': match.start(),
                    'type': chunk_type,
                    'match': match
                })

        # Sort boundaries by position
        boundaries.sort(key=lambda x: x['start'])

        # Second pass: Create chunks
        current_pos = 0
        for i, boundary in enumerate(boundaries):
            # Handle gap between chunks
            if boundary['start'] > current_pos:
                gap_content = content[current_pos:boundary['start']]
                if len(gap_content.strip()) > 0:
                    chunks.extend(self._chunk_by_size(file_path, gap_content, language))  # Pass the 'language' here

            # Find end of current semantic block
            end_pos = (boundaries[i + 1]['start'] 
                    if i + 1 < len(boundaries) 
                    else len(content))

            chunk_content = content[boundary['start']:end_pos]
            if len(chunk_content) > self.config.max_chunk_size:
                sub_chunks = self._chunk_by_size(file_path, chunk_content, language)  # Pass the 'language' here
                chunks.extend(sub_chunks)
            else:
                chunks.append(CodeChunk(
                    text=chunk_content,
                    file_path=file_path,
                    start_line=content[:boundary['start']].count('\n') + 1,
                    end_line=content[:end_pos].count('\n') + 1,
                    chunk_type=boundary['type'],
                    language=language,
                    metadata={'match_groups': boundary['match'].groups()}
                ))

            current_pos = end_pos

        return chunks

    def _chunk_by_size(self, file_path: str, content: str, language: str) -> List[CodeChunk]:
        chunks = []
        lines = content.split('\n')
        current_chunk = []
        current_size = 0
        start_line = 0

        for i, line in enumerate(lines):
            line_size = len(line)
            if current_size + line_size > self.config.max_chunk_size and current_chunk:
                chunks.append(CodeChunk(
                    text='\n'.join(current_chunk),
                    file_path=file_path,
                    start_line=start_line + 1,
                    end_line=i,
                    chunk_type='block',
                    language=language,  # Include language
                    metadata={}
                ))
                current_chunk = []
                current_size = 0
                start_line = i

            current_chunk.append(line)
            current_size += line_size

        if current_chunk:
            chunks.append(CodeChunk(
                text='\n'.join(current_chunk),
                file_path=file_path,
                start_line=start_line + 1,
                end_line=len(lines),
                chunk_type='block',
                language=language,  # Include language
                metadata={}
            ))

        return chunks




def ilovenych():
    # Example usage
    return "Hello, I love you!"