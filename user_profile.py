from database import db_helper

class UserProfile:
    def __init__(self, user_id):
        self.user_id = user_id

    def get_data(self):
        conn = db_helper.get_connection()
        query = """
            SELECT p.*, u.username, u.role
            FROM profiles p
            JOIN users u ON p.user_id = u.id
            WHERE p.user_id = ?
        """
        data = conn.execute(query, (self.user_id,)).fetchone()
        conn.close()
        return data

    def update_profile(self, name, region, bio, email):
        conn = db_helper.get_connection()
        conn.execute("""
            UPDATE profiles
            SET name = ?, region = ?, bio = ?, email = ?
            WHERE user_id = ?
        """, (name, region, bio, email, self.user_id))
        conn.commit()
        conn.close()