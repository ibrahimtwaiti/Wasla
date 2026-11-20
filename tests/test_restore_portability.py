import unittest
from scripts.restore_postgres import portable_schema


class RestorePortability(unittest.TestCase):
    def test_omits_only_top_level_source_attributes(self):
        setting = 'ALTER TABLE ONLY public.example ALTER COLUMN id SET (pg_attver=123);'
        protected = (
            "COMMENT ON TABLE public.example IS 'وصف\n"
            + setting
            + "\n';\n"
            + "SELECT $$\n"
            + setting
            + "\n$$;\n/* multi\n"
            + setting
            + "\n*/\n"
        )
        keep = 'ALTER TABLE ONLY public.example ALTER COLUMN id SET STATISTICS 300;'
        prefix = '\\restrict somekey\nCREATE TABLE public.example (id text);\n'
        sql, removed = portable_schema(
            prefix + setting + '\n' + protected + keep + '\n' + setting + '\n\\unrestrict somekey\n'
        )
        self.assertEqual(removed, 2)
        self.assertEqual(sql, prefix + '\n' + protected + keep + '\n\n\\unrestrict somekey\n')

    def test_stock_schema_is_unchanged(self):
        sql = 'CREATE TABLE public.example (id text);\n'
        self.assertEqual(portable_schema(sql), (sql, 0))
