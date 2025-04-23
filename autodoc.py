class TreeNode:
    def __init__(self, value):
        self.value = value
        self.left = None
        self.right = None

    def preorder_traversal(root):
      if root:
          print(root.value, end=" ")
          preorder_traversal(root.left)
          preorder_traversal(root.right)

    def postorder_traversal(root):
      if root:
          postorder_traversal(root.left)
          postorder_traversal(root.right)
          print(root.value, end=" ")

    def insert(root, value):
      if root is None:
          return TreeNode(value)
      if value < root.value:
          root.left = insert(root.left, value)
      else:
          root.right = insert(root.right, value)
      return root

    def find_height(root):
      if root is None:
          return 0
      return 1 + max(find_height(root.left), find_height(root.right))


    def is_valid_bst(root, min_val=float('-inf'), max_val=float('inf')):
      if root is None:
          return True
      if not (min_val < root.value < max_val):
          return False
      return is_valid_bst(root.left, min_val, root.value) and is_valid_bst(root.right, root.value, max_val)


    def search(root, target):
      if root is None:  # Base case: Tree is empty or value not found
          return False

      if root.value == target:
          return True
      elif target < root.value:
          return search(root.left, target)  # Search in left subtree
      else:
          return search(root.right, target)  # Search in right subtree

    def find_min(root):
      if root is None:
          return None

      while root.left:  # Go as left as possible
          root = root.left
      return root.value


    def find_max(root):
      if root is None:
          return None

      while root.right:  # Go as right as possible
          root = root.right
      return root.value


    def delete(root, value):
      if root is None:
          return root

      if value < root.value:
          root.left = delete(root.left, value)
      elif value > root.value:
          root.right = delete(root.right, value)
      else:
          if root.left is None and root.right is None:
              return None
          if root.left is None:
              return root.right
          elif root.right is None:
              return root.left

          min_val = find_min(root.right)
          root.value = min_val
          root.right = delete(root.right, min_val)

      return root

